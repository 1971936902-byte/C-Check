# RAG 优化措施记录

本文记录 C-Check 在 `C-Check-GpuPlus-RAG` 分支上的 RAG 审查可靠性优化内容、设计目标、具体作用和验证结果。

## 1. 优化目标

本轮优化围绕 C 语言多文件审查中的几个核心问题展开：

- 模型只看当前文件时，容易遗漏被调用函数、头文件声明、宏定义、类型定义和全局变量。
- 大项目批量审查时，直接把文件内容塞进 prompt 会带来上下文浪费。
- 关键证据如果被关键词或向量噪声挤掉，会导致报告定位不准。
- 多文件、多函数、多任务场景下，需要更细粒度的审查单元和可评估的 RAG 指标。

优化原则是：优先增强证据质量和排序稳定性，不盲目扩大 Top-K，不牺牲已有召回和性能。

## 2. 已完成的主要优化

### 2.1 语义解析增强

涉及模块：

- `backend/app/services/code_index/parser.py`
- `backend/app/services/code_index/clangd.py`
- `backend/app/services/code_index/chunker.py`
- `backend/app/services/code_index/graph_builder.py`

优化内容：

- 引入可选的 `tree-sitter-c`、`ctags`、`clangd/libclang` 适配能力。
- 保留内置轻量 C 解析器作为稳定兜底。
- 合并多来源解析结果，避免单一工具缺失符号时影响索引完整性。
- 增强对常见 C 风格函数定义的识别，包括多行函数头、指针返回值、驱动风格宏修饰函数。
- 增加函数指针、回调变量、条件编译符号解析。

作用：

- 提升函数、声明、宏、类型、全局变量、调用点的识别率。
- 为后续 clangd/libclang 深度语义分析提供扩展点。
- 降低真实嵌入式 C 项目中漏建图、漏召回的概率。

### 2.2 知识图谱关系增强

涉及模块：

- `backend/app/services/code_index/graph_builder.py`
- `backend/app/services/code_index/retriever.py`

新增或增强的边类型：

- `FUNCTION_CALLS_FUNCTION`
- `CALLSITE_CALLS_SYMBOL`
- `SYMBOL_DECLARED_IN`
- `SYMBOL_DEFINED_IN`
- `FUNCTION_USES_MACRO`
- `FUNCTION_USES_TYPE`
- `FUNCTION_USES_GLOBAL`
- `FUNCTION_USES_CALLBACK`
- `FUNCTION_DEPENDS_ON_CONDITION`

作用：

- 当前函数调用其他接口时，可以沿调用边找到被调用函数定义。
- 头文件声明可以和 `.c` 文件定义建立关系。
- 宏、类型、全局变量、回调、条件编译信息可以作为审查证据进入 prompt。
- 同名函数场景下优先选择同目录、同文件关系更强的定义，降低误召回。

### 2.3 RAG 切片策略优化

涉及模块：

- `backend/app/services/code_index/chunker.py`
- `backend/app/services/code_index/planner.py`

优化内容：

- 按文件摘要、函数、超大函数滑窗、声明、宏、类型、全局变量、调用点等结构化粒度切片。
- 函数级审查单元会聚合同函数相关的 `function`、`function_window`、`callsite` chunk。
- chunk 元数据补充 `used_macros`、`used_types`、`used_globals`、`used_callbacks`、`used_conditionals` 等字段。

作用：

- 避免纯字符切片破坏 C 语言结构。
- 让审查粒度从文件/chunk 进一步向函数审查单元演进。
- 为后续多 GPU 按函数或调用子图调度打基础。

### 2.4 BM25 / 关键词检索与 Query Expansion

涉及模块：

- `backend/app/services/code_index/keyword_search.py`

优化内容：

- 增加 BM25 风格关键词评分。
- 对函数名、宏名、变量名做拆分和扩展，例如 `helperCopy`、`helper_copy`、`MAX_PACKET_SIZE` 可归一到多个检索词。
- 索引侧和查询侧使用一致的标识符拆分逻辑。
- 修复高价值 chunk 在无关键词命中时仍被加分召回的问题。

作用：

- 提升固定符号名、函数名、宏名的检索准确率。
- 降低无关函数因为类型加权被错误召回的概率。
- 负样本命中率保持为 0。

### 2.5 二次重排优化

涉及模块：

- `backend/app/services/code_index/retriever.py`

优化内容：

- 按证据类型进行重排，优先级大致为：调用图、符号依赖、include、上游调用者、关键词、向量。
- 增加调用距离惩罚，越远的调用链权重越低。
- 增加同文件、同目录、同 stem 文件名相关性权重。
- 增加风险 API 相关性权重。
- 增加标识符重合度权重。
- 增加噪声符号惩罚，例如过短变量名、临时变量名。
- 同一 evidence 同时被调用图和关键词命中时，优先保留调用图/符号等强证据。

作用：

- 关键跨文件函数定义可以稳定排到 Top 1。
- 同名函数时优先选择更相关的文件定义。
- 避免关键词或向量结果覆盖更可靠的图谱证据。

### 2.6 Top-K 噪声压缩和证据预算

涉及模块：

- `backend/app/services/code_index/retriever.py`
- `backend/app/services/code_index/context_builder.py`

优化内容：

- Top-K 分成强证据和弱证据两层。
- 强证据包括 `call`、`symbol`。
- 已有强证据时，`vector` 不再进入最终证据。
- 已有强证据时，`include` 文件摘要不再作为宽泛兜底进入最终证据。
- 同一符号已有强证据时，弱关键词证据不再重复占位。
- 证据预算调整为：图谱 60%、符号 30%、搜索 10%。
- 搜索类证据截断更短，减少 prompt 噪声。

作用：

- 在保持 Recall@10 为 1.0 的情况下，Precision@10 从 0.3333 回升到 0.5000。
- Token Waste Ratio 从 0.6667 降回 0.5000。
- 证据数量从 3 条压缩为 2 条。
- 渲染上下文从 659 字符压缩到 500 字符。

### 2.7 Evidence Prompt 结构化

涉及模块：

- `backend/app/services/code_index/context_builder.py`
- `backend/app/services/model_router.py`
- `backend/app/tasks/reviews.py`

优化内容：

- RAG 上下文以 `RAG Evidence Context` 形式注入。
- 每条证据生成稳定的 `Evidence E编号`。
- prompt 明确要求模型优先基于当前目标和有效 Evidence 判断。
- 后处理校验模型返回的 `evidence_ids` 和 `call_chain`。

作用：

- 模型输出的问题可以关联证据编号。
- 无效证据编号会被过滤。
- 不在图谱中的伪调用链会被清理。

### 2.8 Qdrant / Embedding 适配

涉及模块：

- `backend/app/services/code_index/embeddings.py`
- `backend/app/services/code_index/qdrant.py`
- `backend/app/services/code_index/indexer.py`

优化内容：

- 增加 deterministic hashing embedding。
- 增加 Qdrant upsert/search 适配器。
- 在配置 Qdrant URL 后，可以把 chunk embedding 写入 Qdrant。

作用：

- 当前本地和测试环境可以稳定复现向量结果。
- 为后续接入真实 embedding 模型和 Qdrant 向量检索留好接口。

### 2.9 跨任务缓存

涉及模块：

- `backend/app/services/code_index/parser.py`

优化内容：

- 对 `parse_c_source(relative_path, source_text)` 增加 LRU 缓存。
- 相同路径和源码重复解析时直接复用结果。

作用：

- 重复上传、重复构建索引时减少解析开销。
- 为后续跨任务、跨项目内容 hash 级缓存打基础。

### 2.10 RAG 评估体系和测试集

涉及模块：

- `backend/app/services/code_index/evaluator.py`
- `backend/tests/test_code_index.py`
- `backend/tests/fixtures/rag_projects/*/metadata.json`

新增指标：

- Recall@K
- Precision@K
- MRR
- NDCG@K
- Token Waste Ratio
- Negative Hit Rate
- Evidence Coverage
- Citation Accuracy
- Call Edge Accuracy
- Declaration / Definition Match Rate
- Finding Precision / Recall / F1
- P50 / P95 / P99 Latency

新增测试：

- 跨文件调用定义召回。
- 同名函数相关文件优先。
- BM25 与 query expansion。
- Evidence 去重与预算分配。
- 函数指针、回调、条件编译解析。
- 函数级审查单元聚合。
- 跨任务解析缓存。
- 负样本检索不命中。
- 结构化响应 evidence/call_chain 后处理。

作用：

- RAG 效果从主观观察变成可量化指标。
- 每次优化都可以检测是否牺牲召回、排序或噪声控制。

## 3. 当前关键指标

本地 mock 样例在最近一次 Top-K 噪声压缩后的指标如下：

| 指标 | 结果 |
| --- | ---: |
| Recall@10 | 1.0000 |
| Precision@10 | 0.5000 |
| MRR | 1.0000 |
| NDCG@10 | 1.0000 |
| Token Waste Ratio | 0.5000 |
| 负样本命中率 | 0.0000 |
| Evidence Coverage | 1.0000 |
| Citation Accuracy | 1.0000 |
| 调用边准确率 | 1.0000 |
| 声明/定义匹配率 | 1.0000 |
| 回调边召回 | 1.0000 |
| 条件编译符号/边召回 | 1.0000 |
| 函数级单元聚合率 | 0.5000 |
| Finding Precision | 1.0000 |
| Finding Recall | 0.5000 |
| Finding F1 | 0.6667 |
| 检索延迟 P50 | 5.96 ms |
| 检索延迟 P95 | 11.92 ms |
| 检索延迟 P99 | 15.41 ms |

最近一次 Top 证据为：

| 排名 | 证据类型 | 文件 | 符号 |
| --- | --- | --- | --- |
| 1 | `call:d1` | `src/helpers.c` | `helper_copy` |
| 2 | `symbol:function_uses_macro` | `include/config.h` | `MAX_PACKET_SIZE` |

## 4. 测试结果

最近一次验证结果：

- RAG 专项测试：`20 passed`
- 后端完整测试：`149 passed, 3 warnings`
- 静态编译检查：通过

现有 warning 均为依赖弃用提示：

- `StarletteDeprecationWarning: httpx / TestClient`
- `HTTP_422_UNPROCESSABLE_ENTITY` 弃用提示

这些 warning 不影响本轮 RAG 功能。

## 5. 后续可继续优化方向

建议后续按优先级继续推进：

1. 接入真实 clangd compile commands，提升宏展开、条件编译和类型匹配能力。
2. 建立人工金标集，覆盖真实项目中的正负样本。
3. 引入真实 embedding 模型，并把 Qdrant 检索纳入自动评估。
4. 做跨任务内容 hash 缓存，复用符号、chunk、embedding。
5. 对函数指针、回调、结构体函数表做更深的语义建模。
6. 把审查任务进一步拆成函数审查单元、调用子图审查单元和规则发现审查单元。
7. 在多 GPU 场景下按审查单元调度，提高大任务和小任务混合时的吞吐。

## 6. 2026-06-25 快速默认模式与输出鲁棒性优化

### 6.1 背景问题

真实审查测试显示，RAG 增强后虽然能补充跨文件证据，但默认链路如果过度检索，会把耗时从未接入 RAG 时的约 80 秒放大到数百秒。主要原因是：

- 小文件被拆成大量函数级审查单元，模型调用次数成倍增加。
- 每个函数审查单元都附带 RAG Evidence，证据文本占用 prompt 预算，导致有效代码上下文变少。
- 嵌入式外设寄存器、常见类型别名、宏和断言被过度检索，增加耗时并带来噪声。
- 模型偶发返回非枚举类别、中文类别名、过长 snippet 或额外字段，导致结构化校验失败。

### 6.2 本轮落地策略

本轮将默认审查模式调整为“快速默认模式 + 高价值缺陷优先”。深度多轮审查和更重的 RAG 证据扩展暂时保留为未来的深度模式。

具体优化包括：

1. 小文件整文件审查：小于等于 8KB 的源文件不再函数级切片，直接整文件送入模型。
2. 按需 RAG：默认只保留一跳图谱关系、直接调用、声明、宏、类型和关键字证据，不再默认展开上游调用者、向量兜底和过深调用链。
3. 低价值标识符过滤：`u8/u16/u32`、`uint32_t`、`CAN/DAC/DMA/RCC/GPIO` 等常见类型或外设名不触发缺失符号 RAG。
4. 外设空指针降级：`CAN->`、`DAC->`、`DMA->` 等固定映射寄存器访问引发的空指针类报告默认降级为建议项。
5. 紧凑 JSON 输出：模型只返回 `severity/category/title/file_path/line/confidence`，后端根据定位行自动补充代码片段和报告字段。
6. 单批 findings 上限调整为 4 条：减少生成长度、结构化失败和无效长尾输出。
7. 默认缺陷类别收敛到 8 类：`buffer_overflow`、`pointer_safety`、`memory_safety`、`resource_leak`、`integer_safety`、`input_validation`、`concurrency`、`logic`。
8. 统一输出适配层继续兜底：`ModelOutputSanitizer` 集中处理未知类别、中文类别名、字符串置信度、过长 snippet、缺失字段和额外字段。
9. 上下文预算提升：vLLM `--max-model-len` 调整为 12288，后端 `MODEL_MAX_INPUT_TOKENS` 调整为 10000，`MODEL_MAX_TOKENS` 降为 512。
10. 证据预算压缩：`RAG_CONTEXT_MAX_CHARS` 调整为 3000，避免证据文本挤占代码主体。
11. 代码 chunk 预算放大：`MODEL_CHUNK_MAX_CHARS` 调整为 35000，配合 0.70 安全系数后约 24500 字符，接近 7K 代码 token 目标。

### 6.3 预期收益

- 小文件任务不再因为函数级切片被拆成大量模型调用。
- 默认 RAG 从“尽量多找上下文”调整为“只补必要证据”，降低噪声和耗时。
- 输出结构更短，模型生成更快，结构化响应失败率更低。
- 中高风险报告更聚焦，嵌入式外设寄存器导致的空指针误报减少。
- 12K vLLM 上下文为 RAG 证据和大文件合并审查预留空间，同时仍保持单 GPU 可承受的保守配置。

### 6.4 当前约束

- 当前云服务器只检测到 GPU0，GPU1 服务应保持关闭，避免调度层误判为双 GPU 可用。
- 12K 上下文会增加 KV Cache 显存压力；如果 vLLM 无法稳定监听，应优先降到 10K 或降低并发，而不是牺牲可用性。
- 深度多轮审查可以提高长尾漏洞召回，但耗时会显著增加，建议作为用户主动选择的深度模式。
