# RAG 第二阶段联调与部署优化记录

## 1. 测试范围

- 测试文件：`imgRead.c`、`imgRead_libfuzzer.c`、`imgRead_socket.c`。
- 验证链路：RAG 分块与索引 → 第一阶段审核 → 第二阶段候选格式化 → 报告生成 → 前端接口读取。
- 目标：降低第一阶段推理耗时，修复第二阶段失败，并确认前后端链路能够完整衔接。

## 2. 遇到的问题

### 2.1 GPU 显存不足与推理速度下降

Jina Code Embeddings 1.5B 与 Qwen 14B 同卡部署时，可供 Qwen KV Cache 使用的显存不足，服务出现 `No available memory for the cache blocks`。为使服务启动而给 Qwen 开启 eager 模式并配置 4 GB CPU offload 后，生成速度下降到约 4.7 token/s，一次完整审核耗时 565.508 秒。

### 2.2 第二阶段响应被错误判定为非法 JSON

第二阶段模型实际返回了符合 `FormattedFindingsResponse` 的 `{"findings": [...]}`，但通用 JSON 提取逻辑写死要求顶层必须同时包含 `summary`、`score` 和 `findings`。因此合法响应被拒绝，候选漏洞格式化阶段失败。

### 2.3 单批候选过多导致输出截断

第二阶段一次格式化 30 个候选时，模型输出容易达到 2048 token 上限，形成不完整 JSON。即使解析规则修复，过大的批次仍会造成稳定性问题。

### 2.4 部署配置不一致

- 数据库中的模型 API Key 与 vLLM 服务启动参数存在单字符差异，导致请求返回 HTTP 401。
- 环境文件中的管理员密码与数据库现有账户不一致，影响页面登录验证；该项属于部署数据维护，不涉及本次代码修改。

## 3. 修改点

### 3.1 按响应模型动态校验 JSON 顶层字段

修改 `backend/app/services/model_router.py`：

- `_extract_json_object` 支持传入不同响应契约所需的顶层字段。
- `_parse_typed_response` 从 Pydantic 响应模型中读取必填字段。
- 完整审核响应仍要求 `summary`、`score`、`findings`；第二阶段格式化响应只要求 `findings`。
- 仅对旧的完整审核契约启用截断响应恢复逻辑，避免不同契约被错误补全。

### 3.2 补充回归测试

修改 `backend/tests/test_model_router.py`，增加 `FormattedFindingsResponse` 仅包含 `findings` 时应成功解析的测试，覆盖本次第二阶段失败根因。

### 3.3 降低第二阶段默认批大小

修改 `backend/app/core/config.py`，将 `candidate_format_batch_size` 默认值从 30 调整为 10，降低单批输出被 token 上限截断的概率。

### 3.4 调整服务器显存部署

- RAG 嵌入模型更换为 `jinaai/jina-code-embeddings-0.5b`，向量维度为 896。
- 使用新的 Qdrant collection：`c_check_code_jina_0_5b_896`，避免与旧模型向量维度混用。
- Qwen 关闭 eager 模式及 CPU offload，设置 GPU memory utilization 为 0.88、max model length 为 8192。
- Jina 服务设置 GPU memory utilization 为 0.05、max model length 为 8192、max sequences 为 16。
- API Key 已在服务器部署配置中统一；本文不记录任何密钥。

旧 1.5B 模型和原 Qdrant collection 均保留，可用于回滚。

## 4. 重新测试结果

### 4.1 RAG 索引

| 指标 | 结果 |
| --- | ---: |
| 文件数 | 3 |
| 分块数 | 97 |
| 符号数 | 37 |
| 关系边数 | 217 |
| 写入向量数 | 97 |
| 嵌入模型 | jina-code-embeddings-0.5b |
| 向量维度 | 896 |

### 4.2 完整审核链路

| 指标 | 结果 |
| --- | ---: |
| 任务 ID | `2b10880d-4cc9-4efb-ae5c-7c6b1fa917fa` |
| 报告 ID | `073f03c3-0ab0-4afe-9a78-5b0d20b8dc34` |
| 总耗时 | 67.161 秒 |
| 优化前耗时 | 565.508 秒 |
| 候选漏洞数 | 34 |
| 第二阶段格式化数 | 15 |
| 后端锚点校验拒绝数 | 1 |
| 格式化失败批次 | 0 |
| 最终漏洞数 | 14 |
| 高危 / 中危 | 13 / 1 |

优化后完整任务约快 8.4 倍，耗时下降约 88%。任务查询、报告查询和 Markdown 报告接口均返回 HTTP 200，前端测试 26 项全部通过。

### 4.3 回归验证

- 后端第二阶段相关定向测试：8 项通过。
- 前端自动化测试：26 项通过。
- Qwen、Jina、API、Worker 服务均保持 active，重启次数为 0。
- 稳态 GPU 显存约使用 36.36 GB，剩余约 3.97 GB。

## 5. 后续注意事项

- 更换嵌入模型或向量维度时必须使用新 collection，或完整重建旧 collection。
- 调整 `candidate_format_batch_size` 时需要同时关注候选数量、模型输出 token 上限和 JSON 完整率。
- 模型 API Key、管理员密码等部署凭据不得写入仓库；应由服务器环境变量或密钥管理服务维护。
- 若重新启用 1.5B 嵌入模型，需要重新评估与 Qwen 同卡部署的 KV Cache 余量。
