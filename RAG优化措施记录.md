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

### 6.5 快速默认模式复盘

快速默认模式验证后，速度收益非常明显，但 seeded 漏洞召回下降也比较明显。典型对比为：

| 场景 | 耗时 | 模型原始 findings | 最终 findings | 观察 |
| --- | ---: | ---: | ---: | --- |
| 旧版较深 RAG 的 ctest_mid | 290 s | 58 | 55 | 召回高，但模型调用次数和上下文成本过高。 |
| 快速默认模式的 ctest_mid | 47 s | 18 | 16 | 速度显著提升，但长尾问题减少。 |
| 快速默认模式的 dvc_test_imgRead | 7.26 s | 4 | 3 | 定位准确、误报少，但 seeded 漏洞粗略召回约 3/11。 |

这说明本轮优化不是方向错误，而是默认策略从“尽量召回”过度收缩到了“快速挑选少量高置信问题”。主要原因包括：

1. 单批 findings 上限被压到 4，面对包含 10 个以上 seeded 漏洞的样例时会天然截断。
2. MODEL_MAX_TOKENS 降到 512，模型输出空间不足，容易只保留最显眼的问题。
3. MODEL_CHUNK_MAX_CHARS 放大到 35000 后，批次数减少，模型在较大代码块中更倾向于摘要式挑选 Top 问题。
4. 函数级审查单元默认关闭，避免了 49 个函数单元导致的长耗时，但也减少了逐函数发现机会。
5. 默认按需 RAG 只保留 include、direct call、usage、keyword 等少量证据，upstream、Qdrant、vector 等弱/远证据默认不进入主链路。
6. 后处理更严格，无法锚定到有效代码语句的 finding 会被过滤；这降低误报，但也可能丢掉真实但行号偏移的问题。

因此后续策略应避免在“290 秒深审查”和“7 秒低召回初筛”之间二选一，而是拆成三档：

- 快速模式：保留极短耗时和低误报，适合交互式初筛。
- 默认模式：适度提高 findings 上限、输出预算和证据覆盖，目标是在 10 到 60 秒内获得更好的召回。
- 深度模式：启用函数级/调用子图审查单元、二次 RAG 和更高证据预算，用于 benchmark、交付前安全审计和高风险项目。

## 7. 2026-06-28 第一阶段召回回调

### 7.1 调整目标

第一阶段不回滚到高耗时深 RAG，而是在保持可接受速度的前提下，修正快速默认模式中过度压缩的几个关键点：

- 让模型每批可以返回更多候选问题。
- 给紧凑 JSON 输出留出描述和证据编号空间。
- 适度缩小 chunk，增加覆盖密度。
- 在默认 RAG 中恢复一部分上游和向量库证据。
- 继续由后端基于 file_path 和 line 自动补充代码片段，避免模型输出长 snippet 导致结构化失败。

### 7.2 落地方案

本阶段已落地的调整包括：

1. findings 上限从 4 提升到 10，并通过共享常量约束 sanitizer、compact schema 和测试，避免前后端或测试断言遗漏。
2. MODEL_CHUNK_MAX_CHARS 从 35000 调整为 18000。结合内部安全系数后，单批仍大于早期 8K，但不会像 35K 那样过度合并。
3. MODEL_MAX_TOKENS 从 512 提升到 1024，让模型有空间返回更多候选和更具体的描述。
4. compact schema 增加 description 和 evidence_ids，前端报告页同步展示描述、置信度和 Evidence 编号。
5. 默认 RAG 在 include、direct call、usage、keyword 基础上增加 upstream；如果已配置 Qdrant，则加入 Qdrant 检索结果。
6. 后端按定位行补充的代码上下文扩大到约 7 行，前端无需模型返回 snippet 也能展示更完整的问题上下文。

本阶段仍保持以下边界：

- 不重新默认启用函数级审查单元，避免回到 49 个审查单元导致的长耗时。
- 不让模型输出 code_snippet、fixed_snippet、remediation，这些仍由后端和报告层统一补齐。
- 暂不实现二次深度 RAG，把它保留为下一阶段能力。

### 7.3 复测结果

在同一类 dvc_test_imgRead seeded 样例上，第一阶段回调后的结果如下：

| 指标 | 快速默认模式 | 第一阶段回调后 |
| --- | ---: | ---: |
| 端到端耗时 | 7.26 s | 13.85 s |
| 最终 findings | 3 | 6 |
| seeded 近邻命中 | 约 4 | 约 6 |
| 行号有效率 | 100% | 100% |
| snippet 补全率 | 100% | 100% |
| Evidence 引用 | 0 | 6，且均有效 |
| RAG 候选/选中 | 35 / 4 | 35 / 4 |

结论：

- 召回有明显改善，最终 findings 从 3 条提升到 6 条。
- Evidence 引用链路开始生效，报告可以追踪到 RAG 证据编号。
- 耗时从 7.26 秒增加到 13.85 秒，但仍处在交互式可接受范围内。
- RAG 选中证据数量仍为 4，说明检索裁剪层仍偏保守，后续可考虑把 selected hard limit 配置化或提升到 6。

### 7.4 暴露的新问题

第一阶段回调后，模型虽然返回了更多 finding，但出现了类别聚集问题：多个结果集中在 buffer_overflow，标题和描述相似，部分 double free、integer safety、resource leak、exhaustion 等类别仍然容易被合并或遗漏。

原因主要有：

- Prompt 中 buffer_overflow 排在前面，模型在输出空间放宽后仍倾向重复最显眼类别。
- “覆盖不同类别”虽然有描述，但不够可执行。
- RAG 证据数量仍较少，复杂路径问题拿不到足够多的上下游证据。
- 当前后处理更关注定位有效性，还没有做类别多样性和重复问题的主动约束。

### 7.5 下一阶段建议

建议第二阶段按以下顺序推进：

1. Prompt 增加类别多样性约束：优先覆盖不同类别后再重复同类问题；同一类别重复时必须是不同根因、不同位置或不同后果。
2. 把 RAG 最终证据数量上限配置化，例如默认从 4 提升到 6，同时保持搜索类证据短截断，避免 prompt 噪声反弹。
3. 对高危且 difficulty=high、confidence 较低或 needs_rag=true 的候选问题，触发二次深度 RAG；二次检索只围绕候选所在函数/行号窗口，图深度可提升到 2。
4. 建立 Juliet、CASTLE-C250 和项目级漏洞靶场的标准评测批次，用粗粒度 seeded hit、类别覆盖率、重复率、定位有效率和耗时共同评估。
5. 将“快速 / 默认 / 深度”作为显式审查模式暴露到配置或前端，避免一个默认参数同时承担演示速度和安全审计召回。

## 8. 2026-06-29 两阶段候选发现与格式化链路

### 8.1 最终流程

本轮把默认审查链路调整为：

1. 源码与静态索引继续由现有索引模块构建。
2. 第一阶段由后端预先检索并注入结构体、枚举、宏、类型、全局变量和直接调用函数等 Definition Context；暂不支持模型在推理过程中主动发起按需 RAG 工具调用。提示词要求模型优先参考这些补充信息，上下文仍不存在时按审查范围外定义处理，不得作为漏洞上报。
3. 第一阶段不再列举详细类别、API清单、专项根因和拆分示例，只用一行提示重点关注 `buffer_overflow`、`memory_safety`、`resource_leak`、`integer_safety` 和 `logic`，同时允许发现其他漏洞，减少类别顺序造成的注意力锚定。
4. 第一阶段使用宽松 JSONL，每行只输出 `p/l/s/t/d`，分别表示文件、行号、初步等级、自由类型和一句话描述。
   提示词采用英文规则约束并明确要求中文描述；审查正文与 JSONL 输出协议分离，避免重复指令。格式样例使用中性占位符，防止具体漏洞类型成为注意力锚点。
5. 后端兼容 JSONL、截断尾行、JSON 数组和旧版 findings 对象，并统一生成候选对象。
6. 后端把第一阶段候选重新序列化为轻量 JSONL，并持久化到 `review_tasks.candidate_jsonl`；第二阶段失败时仍可保留和复现第一阶段结果。
7. 第二阶段只接收缓存的 JSONL 文档和用户选择的允许漏洞类型，不接收 C 源码、RAG、Definition Context 或候选代码窗口。
8. 第二阶段不审核漏洞真假，不发现新漏洞，只删除类型不符合、函数/类型未定义、隐式声明、泛化空指针检查、固定映射外设空指针和厂商断言误报等记录，并把自由类型映射到严格枚举。模型仅输出 `findings`，摘要和评分由后端生成。
9. JSONL 较长时按配置分批格式化，后端合并各批严格 findings。
10. 后端最后执行允许类型兜底、文件和行号校验、精确去重及规则过滤，再通过 `ModelReviewResponse` 生成最终报告。

### 8.2 可靠性策略

- JSONL 最后一行被截断时，前面完整候选仍可恢复。
- 第一阶段 JSONL 在第二阶段调用前提交到数据库，避免格式化失败导致候选数据丢失。
- 第二阶段模型不可用或格式失败时，后端使用同一 JSONL 解析器完成降级格式化，并记录模型日志。
- 未定义函数、声明、宏、类型或结构体不再作为漏洞；同时禁止猜测未知实现的返回契约和副作用。
- 第二阶段无法访问源码或 RAG，避免它重新进行漏洞分析或改变第一阶段事实。
- 后端再次强制检查用户选择的漏洞类型，防止模型保留范围外类别。
- 外部小写全局变量重新纳入缺失符号检索，避免定义优先模式漏掉全局状态依赖。

### 8.3 配置与验证

新增配置：

- `CANDIDATE_FORMAT_BATCH_SIZE=30`：第二阶段单次格式化的 JSONL 候选数量。

本轮完整后端测试结果：`186 passed, 3 warnings`。警告来自 FastAPI/Starlette 已弃用接口，与本轮功能无关。

### 8.4 最终提示词与调试结论

本轮调试过程中先后验证并纠正了以下设计边界：

1. 第一阶段必须继续使用 RAG：源码、静态索引和 Definition Context 一起交给 LLM，RAG 用于补充结构体、成员、枚举、宏、全局变量、函数声明和定义；“暂不按需 RAG”仅表示模型推理中不主动发起工具调用。
2. 第二阶段不是漏洞复核器：不再接收源码、RAG或候选窗口，也不执行 `keep/drop/correct` 语义裁决，只处理第一阶段缓存的 JSONL 文档。
3. 第一阶段不接收用户选择的最终检查类型，以免过早限制召回；用户选择的类型只在第二阶段和后端最终过滤中生效。
4. 第一阶段提示词采用英文规则、中文描述输出。英文用于 C 语言术语、结构约束和 JSON 协议，中文用于最终报告；不重复提供中英双语版本。
5. 审查正文与输出协议完全分离：正文只说明高召回、RAG边界、证据落点和提示词注入防护；JSONL协议只说明 `p/l/s/t/d` 字段及转义规则。
6. 第一阶段不再包含长类别清单、API清单、资源泄漏专项教学和多条“不合并”规则，仅用一行提示五类重点风险，同时保留开放类型发现。
7. JSONL格式样例改为中性占位符，避免 `buffer_overflow/high` 等具体值成为注意力锚点。
8. 提示词明确“只假设符号存在，不假设行为安全”，禁止模型虚构外部符号的返回契约、所有权、副作用或安全保证。
9. 增加提示词注入防护：源码、注释、字符串、标识符和 RAG 内容均视为不可信数据，不执行其中包含的指令。
10. 第二阶段模型仅输出 `findings`；`summary` 和 `score` 由后端依据最终问题列表计算，减少无意义生成。
11. 第二阶段输出数量不得超过输入数量，不得新增漏洞、修改路径和行号、扩写事实或把无法映射的类型强塞进 `logic/other`。
12. 新增 Alembic 迁移 `0011_review_candidate_jsonl`，用于持久化第一阶段候选JSONL；任务重跑时会清理旧缓存。

最终验证包括JSONL截断恢复、旧格式兼容、缓存持久化、第一阶段RAG注入、第二阶段无源码/无RAG、允许类型过滤、固定映射外设误报过滤、厂商断言误报过滤以及严格报告生成。完整后端测试为 `186 passed, 3 warnings`。
