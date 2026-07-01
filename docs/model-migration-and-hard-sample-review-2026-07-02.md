# 32B FP8 模型迁移与困难漏洞样例复盘

记录日期：2026-07-02

## 1. 模型迁移背景

原线上审查模型为 Qwen2.5-Coder 14B FP8。为了验证更大稠密模型在复杂 C 语言漏洞、宏陷阱、跨函数推理和 RAG 证据利用方面的能力，本次将线上模型切换为用户已下载的 32B FP8 模型：

```text
/opt/modelscope/okwinds/Qwen2.5-Coder-32B-Instruct-FP8
```

目标：

- 使用现有云服务器模型目录，不重新下载权重。
- 保持当前后端、Worker、RAG、Qdrant 与前端链路不变。
- 将后端默认可用模型节点切换到 32B FP8。
- 完成 vLLM 真实生成验证、后端模型节点更新和前后端服务重启。

## 2. 服务器迁移步骤

### 2.1 环境确认

在服务器上确认以下信息：

- 模型目录存在：`/opt/modelscope/okwinds/Qwen2.5-Coder-32B-Instruct-FP8`
- 模型配置为 `Qwen2ForCausalLM`
- 权重量化为 dynamic FP8
- `max_position_embeddings=32768`
- 当前服务器 GPU 为单卡 RTX 4090 48GB
- 现有服务包含：
  - `c-check-vllm-qwen`
  - `c-check-vllm-jina`
  - `c-check-api`
  - `c-check-worker`
  - `qdrant`
  - `nginx`

### 2.2 备份并修改 vLLM systemd 服务

备份当前服务文件：

```bash
cp /etc/systemd/system/c-check-vllm-qwen.service \
   /etc/systemd/system/c-check-vllm-qwen.service.bak-$(date +%Y%m%d%H%M%S)
```

关键修改项：

```text
--model /opt/modelscope/okwinds/Qwen2.5-Coder-32B-Instruct-FP8
--served-model-name qwen2.5-coder-32b-instruct-fp8
--gpu-memory-utilization 0.84
--max-model-len 8192
--max-num-seqs 2
--kv-cache-dtype fp8
```

说明：

- 32B FP8 在单卡 4090 48GB 上可以加载，但显存余量有限。
- 当前先保持 `max-model-len=8192`，避免与 Jina embedding 服务、Qdrant、API 和 Worker 同机运行时触发 OOM。
- 后端输入预算仍应小于 vLLM 上下文长度，保留给系统提示词、RAG 证据和输出 token。

### 2.3 重载并启动模型服务

```bash
systemctl daemon-reload
systemctl restart c-check-vllm-qwen
journalctl -u c-check-vllm-qwen -f
```

验证模型 API：

```bash
curl http://127.0.0.1:8001/v1/models
curl http://127.0.0.1:8001/v1/chat/completions
```

本次验证结果：

- `/v1/models` 正常返回。
- 真实 chat completion 成功返回 `MODEL_OK`。
- vLLM served model 为 `qwen2.5-coder-32b-instruct-fp8`。
- 模型加载后显存占用约 41GB，剩余约 7.5GB。

### 2.4 更新数据库模型节点

将默认 Qwen 节点更新为：

```text
display_name: Qwen2.5-Coder 32B Instruct FP8
identifier: qwen2.5-coder-32b-instruct-fp8
base_url: http://127.0.0.1:8001
gpu_indices: [0]
tensor_parallel_size: 1
timeout_seconds: 600
enabled: true
```

同时禁用重复的旧 Qwen 节点，保留 Mock 节点用于页面和流程测试。

### 2.5 重启业务服务并验证

```bash
systemctl restart c-check-api
systemctl restart c-check-worker
nginx -t && systemctl reload nginx
systemctl is-active c-check-api c-check-worker c-check-vllm-qwen c-check-vllm-jina qdrant nginx
```

验证结果：

- 后端 API：`http://127.0.0.1:8000/docs` 返回 200。
- 前端静态页面：`http://127.0.0.1:8800/` 返回 200。
- 公网映射页面返回 200。

## 3. 困难漏洞样例测试结论

用户使用一个相对困难的 C 语言漏洞样例文件测试 32B FP8 模型能力，结果如下。

### 3.1 准确识别的缓冲区越界漏洞

1. `read_single_line` 行读取栈溢出

```c
if (idx > buf_max) break;
```

边界判断缺少等号。当输入长度刚好等于 `buf_max` 时，后续写入会导致栈缓冲区越界。模型命中准确。

2. `batch_format_output` 导出栈溢出

```c
if (write_pos + line_len > export_max) break;
```

条件同样缺少等号。拼接总长度刚好填满 `export_buf[512]` 时，`strcpy` 会写到下标 512，触发栈越界。模型命中准确。

3. `split_multipart_text` 中 `STR_SAFE_COPY` 堆拷贝溢出

模型识别到了拷贝溢出风险，但只命中表象，未定位宏定义本身缺少防护的根因：

```c
#define STR_SAFE_COPY(dst, src, len) do{ memcpy(dst, src, len); }while(0)
```

当 `len` 传入复合表达式时，宏展开、运算优先级和长度计算可能导致拷贝长度失控。当前模型只报了表层 `memcpy` 风险，未完整串联宏预处理根因。

4. `load_log_page` 中 `strcpy` 无长度限制堆溢出

```c
strcpy(mgr->entry_list[loaded].msg_content, parts[3]);
```

模型能识别 `strcpy` 无长度限制带来的高危风险。该类问题属于模型较稳定覆盖的基础缓冲区安全问题。

### 3.2 准确识别的整数安全和逻辑缺陷

1. `parse_log_number` 有符号整数溢出

循环中 `res = res * 10 + digit` 在超出 `INT32_MAX/MIN` 时会触发有符号整数溢出 UB。模型识别准确。

2. `calc_data_range` 极值判断逻辑失效

当 `min=-2147483648`、`max=2147483647` 时，`max - min` 超出 int32 范围，赋值给 `uint32_t diff` 后导致溢出检查失效。模型将其归为 logic 类问题，判定合理。

### 3.3 主要误报：resource_leak 资源泄漏

本次报告中多处 `resource_leak` 告警属于误报：

- `char* parts[4]` 已在 `free_string_array(parts, 4)` 中释放。
- `export_buf[512]` 是栈局部数组，不需要也不能手动释放。
- 全局文件句柄 `log_fp` 和分页管理器 `mgr` 在 `main` 末尾已有释放流程。

误报原因：

- 模型容易把局部数组、临时变量和真实堆资源混淆。
- 对生命周期闭环理解不稳定，尤其是释放逻辑不在同一局部片段时。
- RAG 证据若没有明确提供资源创建和释放路径，模型会倾向于保守上报。

当前建议：

- 对 `resource_leak` 增加后端规则复核，至少区分栈对象、全局对象、堆对象和文件句柄。
- 对已出现配对释放函数的候选降低置信度或降级为建议。
- 在提示词中继续强调“栈局部数组不是资源泄漏，只有缺失释放路径的堆资源、文件句柄、锁、描述符等才上报”。

### 3.4 漏检的高危隐蔽漏洞

1. `page_mgr_create` 堆分配尺寸无符号乘法溢出

```c
mgr->entry_list = malloc(page_size * sizeof(LogEntry));
```

传入超大 `page_size` 时，`uint32_t` 乘法可能溢出，导致分配过小，后续写入触发堆越界。模型未识别。

2. `CALC_PAGE_OFFSET` 分页偏移宏无括号和乘法溢出

```c
#define CALC_PAGE_OFFSET(page, per) (page * per)
uint64_t skip_bytes = CALC_PAGE_OFFSET(target_page, PAGE_ITEM_MAX) * 128;
```

模型未完整识别“宏定义 -> 宏展开 -> 整数溢出 -> 文件 IO 非法偏移”的链路。

3. `read_single_line` 结束符二次越界

模型命中了循环内写入越界，但漏掉了循环退出后：

```c
out_buf[idx] = '\0';
```

当 `idx == buf_max` 时，结束符写入也会发生二次越界。

## 4. 本次测试体现出的能力边界

### 4.1 优点

- 基础栈/堆缓冲区边界错误识别稳定。
- `strcpy`、`memcpy` 等高风险函数识别稳定。
- 简单有符号整数溢出和基础逻辑判断失效可以命中。
- 能识别 `>` 缺少 `=` 这类细微边界缺陷。
- `buffer_overflow`、`integer_safety`、`logic` 等分类整体清晰。

### 4.2 短板

- 宏预处理深层陷阱识别弱，尤其是宏定义缺少括号、宏内乘法溢出、宏展开后的真实表达式风险。
- 无符号乘法溢出和堆分配尺寸溢出仍是明显漏检点。
- 对资源泄漏的生命周期判断粗糙，容易把栈对象和已释放对象误报。
- 对同一函数内的连续风险点可能合并，只标记首个表象，不继续追踪结束符写入、后续写入等二次影响。
- 对“宏定义 -> 调用点 -> 上层函数崩溃”的完整证据链构造不足。

## 5. 后续优化方向

### 5.1 静态规则补强

优先在后端增加确定性规则，而不是完全依赖模型：

- `malloc/calloc/realloc` 尺寸表达式溢出检查。
- `a * sizeof(T)`、`count * element_size`、`page * per * stride` 等乘法链溢出检查。
- 边界条件 `>` 与 `>=`、`<` 与 `<=` 的 off-by-one 模式检查。
- `buf[idx] = '\0'` 结束符写入二次越界检查。
- 宏定义参数缺少括号、宏整体表达式缺少括号检查。

### 5.2 RAG 证据优化

RAG 不应只提供函数局部片段，还应为高风险候选补充：

- 宏定义原文和宏展开前后的表达式。
- 类型宽度，例如 `uint32_t`、`size_t`、`int32_t`。
- 分配点、写入点和释放点之间的短链路。
- 直接调用的一跳声明，而非无关上下文。

### 5.3 第二阶段候选复核

对第一阶段候选增加更强的规则过滤：

- `resource_leak` 必须存在明确资源创建点，且没有可见释放路径。
- 栈数组不得作为资源泄漏上报。
- 已有 `free/fclose/destroy/unlock` 等释放路径时，默认降级或要求更高证据。
- 对宏和整数溢出候选增加静态证据加权，避免被模型遗漏。

### 5.4 提示词补充

第一阶段提示词可以继续保持简洁，但需要加一条通用覆盖规则：

```text
Pay special attention to size calculations used by malloc/calloc/realloc, file offsets, array indexes, and copy lengths, including unsigned multiplication overflow, macro-expanded expressions, and off-by-one terminator writes.
```

资源泄漏提示可补充：

```text
Do not report stack arrays or local non-owning variables as resource leaks. Report resource_leak only when a heap allocation, file handle, descriptor, lock, or similar owned resource has a visible missing release path.
```

## 6. 结论

32B FP8 模型相比 14B 更适合承担复杂候选发现任务，基础缓冲区、简单整数溢出和逻辑缺陷识别能力较好。但工业级 C 代码审查不能只依赖模型本身。宏展开、无符号尺寸溢出、堆分配尺寸溢出、资源生命周期闭环等问题，需要“静态规则 + RAG 证据 + LLM 候选发现 + 后端二次复核”共同完成。

当前最值得优先落地的改进是：

1. 增加 malloc 尺寸溢出和宏表达式风险的静态规则。
2. 对 resource_leak 做确定性降噪。
3. 在 RAG 证据中补充宏定义、类型宽度、分配-写入-释放链路。
4. 对结束符写入和 off-by-one 风险做专门规则。
5. 将上述能力纳入回归测试集，避免模型或提示词调整后能力退化。

## 7. 2026-07-02 首轮落地修复

针对本次困难样例暴露的 8 类问题，已先落地低成本、高确定性的后端补强：

- 新增静态补充候选规则，覆盖函数式宏参数缺少括号、内存分配尺寸乘法溢出、有符号累乘溢出、无符号差值溢出、边界缺少等号、字符串结束符二次越界、边界后不安全拷贝。
- 候选合并阶段将静态规则结果与 LLM 第一阶段候选合并，再进入既有格式化、锚定、去重和类型过滤流程。
- 增加 `resource_leak` 降噪逻辑：栈数组、局部非 owning 变量、同函数内可见释放路径的对象，不再作为资源泄漏保留。
- 第一阶段提示词轻量补充尺寸计算、宏展开、结束符写入和资源泄漏边界，避免模型把局部栈对象误报为资源泄漏。
- 增加回归测试，固定覆盖困难样例中的宏陷阱、分配尺寸溢出、结束符二次越界，以及资源泄漏误报过滤。

同一困难样例输入的规则层验证结果：

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 静态补充候选数量 | 0 | 9 |
| 覆盖 `STR_SAFE_COPY` 宏参数风险 | 否 | 是 |
| 覆盖 `CALC_PAGE_OFFSET` 宏参数风险 | 否 | 是 |
| 覆盖 `malloc(page_size * sizeof(LogEntry))` 尺寸溢出 | 否 | 是 |
| 覆盖 `out_buf[idx] = '\0'` 二次越界 | 否 | 是 |
| 覆盖 `write_pos + line_len > export_max` 边界缺等号 | 依赖模型 | 是 |
| 明显规则噪声 | 1 条初版噪声 | 已收紧去除 |

验证：

- `python -m pytest backend/tests/test_model_router.py -q`：63 passed。
- `ALLOW_INSECURE_DEFAULTS=true python -m pytest backend/tests -q`：211 passed，3 warnings。

后续仍建议继续补充：

- 宏展开后的真实表达式构造与展示。
- 分配点、写入点、释放点的跨函数短链路校验。
- 更完整的资源生命周期规则，例如 `goto cleanup`、多出口路径和条件释放。
- 针对标准漏洞集的固定金标评估，把本次“规则层改善”转化为端到端 Recall/Precision 指标。
