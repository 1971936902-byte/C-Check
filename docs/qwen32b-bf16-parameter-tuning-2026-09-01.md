# Qwen32B BF16 参数调优实验记录

## 目标

在当前 2 x A100 40GB、Qwen2.5-Coder-32B-Instruct BF16、KV Cache auto/BF16 路线下，用单一变量法优化审查效率、准确度和抗过拟合能力。

本轮实验不以“报告数量越多越好”为目标，而以以下结果为准：

- 高价值漏洞召回稳定，尤其不能漏掉 `ServiceProtocolRecv` 的外部长度驱动写入风险。
- 明显误报、低级报错、重复报和行号漂移减少。
- 单文件任务耗时和全量任务耗时可接受。
- 参数每次只改变一组，上一组确定最优后再进入下一组。

## 固定测试集

### T1: ServiceProtocol.c

路径：`C:\Users\19719\Desktop\E5_Wireless\ServiceProtocol.c`

选择原因：

- 包含协议长度解析、加密/解密分支、`ptr->uMessageLen`、`uDataPar`、`WL_ReceiveBytes` 等高价值链路。
- 曾出现关键漏报：`ServerProtocolRecv` 未校验 `ptr->uMessageLen` 导致缓冲区溢出风险。
- 适合验证复杂上下文、根因行与 sink 行定位能力。

核心观察点：

- 必须识别 `ServerProtocolRecv` 长度边界风险。
- 不能把已证明安全的 DES/RSA 固定块逻辑大量误报。
- 相邻根因需要合并展示，避免多行重复刷屏。

### T2: WirelessModule_EC600U.c

路径：`C:\Users\19719\Desktop\E5_Wireless\WirelessModule_EC600U.c`

选择原因：

- 包含 AT 命令拼接、`strstr` 后续判空、文件数据接收、固定缓冲区和业务错误码。
- 曾出现 `strstr` 已判空误报、`Err_Type` 被误判为未初始化指针、`memset(sizeof(pointer))` 分类偏重等问题。
- 适合验证误报抑制、低级问题降级、过拟合和行号准确性。

核心观察点：

- `strstr` 已判空不应作为高级漏洞。
- `Err_Type` 这类业务错误码不应被高危/中危指针问题污染。
- `Quectel_EC600U_CN_GetFileData` 中长度计算与 `memcpy` 风险应保持可见。

## 固定基线

除当前实验组正在调整的变量外，其余配置固定如下：

- 模型：Qwen2.5-Coder-32B-Instruct BF16
- GPU：2 x NVIDIA A100-SXM4-40GB
- Tensor Parallel：2
- KV Cache：auto/BF16 路线
- `max_model_len=8192`
- `max_num_seqs=1`
- `max_num_batched_tokens=8192`
- Prefix cache：开启
- 检查类型：全部 6 项，前端文案已收敛为高价值目标
- 知识图谱 supplemental 持久化：实验期间关闭，仅构建上下文不写入持久化图谱

## 评估指标

每个测试文件每档记录：

| 指标 | 说明 |
| --- | --- |
| 总耗时 | 任务 `duration_ms`，并拆分 candidate / format / validation / merge / RAG supplemental |
| 报告数量 | 最终 finding 数和 finding group 数 |
| 高价值命中 | 已知关键漏洞是否出现 |
| 明显误报 | 已判空、固定容量可证明安全、业务常量误判等 |
| 重复率 | finding 数与 group 数差值，以及同函数相邻重复 |
| 行号准确性 | 是否落在根因行或真实 sink 行，是否落到 `break/#ifdef/return` 等无效行 |
| 格式稳定性 | 是否出现 model formatting failed / fallback |
| 性能状态 | vLLM 平均吞吐、GPU 利用率、显存、是否出现长时间空转 |

## 实验顺序

### G1: n-gram 保守参数

固定其他参数，只调整 vLLM speculative n-gram。

| 档位 | 参数 | 预期 |
| --- | --- | --- |
| A | 关闭 n-gram | 稳定基线，速度最慢 |
| B | `num_speculative_tokens=3, prompt_lookup_min=6, prompt_lookup_max=8` | 均衡档，当前优先测试 |
| C | `num_speculative_tokens=2, prompt_lookup_min=8, prompt_lookup_max=10` | 更保守，误匹配更少但加速弱 |

通过标准：

- `ServiceProtocolRecv` 风险必须命中。
- `WirelessModule_EC600U.c` 的 `strstr` 判空误报不能回潮。
- 报告数量不应出现异常大幅波动。
- 若 B 与 C 准确性接近，优先选择耗时更低者。

### G2: 输出 token 预算

在 G1 最优档基础上，只调整候选与格式化输出预算。

| 档位 | 参数 | 预期 |
| --- | --- | --- |
| A | `CANDIDATE_MODEL_MAX_TOKENS=1536, MODEL_MAX_TOKENS=1536` | 更少啰嗦和重复 |
| B | `CANDIDATE_MODEL_MAX_TOKENS=2048, MODEL_MAX_TOKENS=2048` | 当前基线 |
| C | `CANDIDATE_MODEL_MAX_TOKENS=3072, MODEL_MAX_TOKENS=2048` | 候选召回更强，可能更慢 |

### G3: 候选动态 token 估算

在 G1/G2 最优档基础上，只调整第一阶段动态 token 估算。

| 档位 | 参数 | 预期 |
| --- | --- | --- |
| A | `tokens_per_line=3.5, dangerous=20, pointer=2` | 更克制，降低重复 |
| B | `tokens_per_line=4.5, dangerous=24, pointer=4` | 当前基线 |
| C | `tokens_per_line=5.5, dangerous=32, pointer=4` | 增强复杂文件召回 |

### G4: RAG 上下文量

在前面最优档基础上，只调整 RAG 上下文规模；仍关闭 supplemental 持久化。

| 档位 | 参数 | 预期 |
| --- | --- | --- |
| A | `RAG_KEYWORD_TOP_K=5, RAG_CONTEXT_MAX_CHARS=2200` | 降低偏题和过拟合 |
| B | `RAG_KEYWORD_TOP_K=8, RAG_CONTEXT_MAX_CHARS=3000` | 当前基线 |
| C | `RAG_KEYWORD_TOP_K=12, RAG_CONTEXT_MAX_CHARS=4000` | 增强跨定义补偿，可能引入噪声 |

### G5: 格式化批大小

在前面最优档基础上，只调整第二阶段候选格式化批大小。

| 档位 | 参数 | 预期 |
| --- | --- | --- |
| A | `CANDIDATE_FORMAT_BATCH_SIZE=6` | 更稳，格式失败概率低但更慢 |
| B | `CANDIDATE_FORMAT_BATCH_SIZE=10` | 当前基线 |
| C | `CANDIDATE_FORMAT_BATCH_SIZE=16` | 更快，可能格式失败或压缩错误 |

## 当前进度

- [x] 确认服务器当前仍有 `E5_Wireless` 任务运行，暂不重启 vLLM。
- [x] 预置保守 n-gram 启动脚本，等待任务结束后重启生效。
- [x] 新增 `RAG_SUPPLEMENTAL_PERSIST_ENABLED` 开关，用于实验期间关闭 supplemental 图谱持久化。
- [x] 固定两个测试文件：`ServiceProtocol.c`、`WirelessModule_EC600U.c`。
- [x] 部署 RAG supplemental 持久化开关到服务器，并设置 `RAG_SUPPLEMENTAL_PERSIST_ENABLED=false`。
- [x] 当前任务结束后重启 vLLM，使 G1-B 参数生效。
- [x] 运行 G1-A/B/C，选择 n-gram 最优档。
- [ ] 继续 G2-G5 单一变量实验。

## 实验结果记录

### G1: n-gram

| 档位 | 文件 | 任务 ID | 耗时 | findings/groups | 关键命中 | 明显误报 | 行号准确性 | 格式稳定性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | T1 | `a53c185b-cdf6-4416-8561-851159775207` | 100.9s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `397/398` 固定发送缓冲区仍偏可疑；`262/333/453` 需复核容量 | 行号整体准确，根因行 `659` 正确 | 无格式失败；format 49.8s | 稳定但偏慢，作为关闭 n-gram 基线 |
| A | T2 | `63255822-68ac-4338-90c8-6b05f42fb1e8` | 71.4s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 为中危建议类仍可再降级 | 行号可用，`112/108/474/721/1105/1252` 等落点清晰 | 无格式失败；format 33.6s | 稳定，低级误报已明显收敛 |
| B | T1 | `dc055733-4cee-44b8-860f-b3db15790b77` | 超过 5min 后中止 | 未完成 | 第一阶段已有候选，但未产出最终报告 | 未完成 | 未完成 | 95% 长时间停留；guided JSON 格式化请求反复进入 outlines 降级路径 | 不可接受，淘汰 |
| B | T2 | 未运行 | - | - | - | - | - | - | B 档 T1 已暴露结构性异常，停止继续扩大样本 |
| C | T1 | `3811619f-fd19-45dc-a21b-e51d2282e592` | 超过 3min 后中止 | 未完成 | 第一阶段已有候选，但未产出最终报告 | 未完成 | 未完成 | 95% 长时间停留；现象与 B 档一致 | 不可接受，淘汰 |
| C | T2 | 未运行 | - | - | - | - | - | - | C 档 T1 已复现异常，停止继续扩大样本 |

G1-A 详细记录：

- T1 pipeline：`discovered=31; formatted=31; backend_rejected=21; proven_safe_buffer=20; final=8; timing_finalize_total_s=50.0406`
- T2 pipeline：`discovered=21; formatted=21; duplicate_roots=9; backend_rejected=3; final=9; timing_finalize_total_s=33.6877`
- 关闭 supplemental 持久化后，收尾阶段不再出现额外图谱写入耗时；后续 G1-B/C 可直接对比 n-gram 对模型阶段和 format 阶段的影响。

G1-B/G1-C 异常记录：

- B 档启动参数已由 vLLM 日志确认：`speculative_config={'method': 'ngram', 'num_speculative_tokens': 3, 'prompt_lookup_min': 6, 'prompt_lookup_max': 8}`。
- C 档启动参数已由 vLLM 日志确认：`speculative_config={'method': 'ngram', 'num_speculative_tokens': 2, 'prompt_lookup_min': 8, 'prompt_lookup_max': 10}`。
- 两档都触发 vLLM 日志：`ngram is experimental on VLLM_USE_V1=1. Falling back to V0 Engine`。
- 两档在第一阶段候选扫描均可推进到 95%，并能看到多次 `/v1/chat/completions` 200 OK。
- 两档都在第二阶段候选格式化请求上长时间停留，日志显示 guided JSON schema 因 `xgrammar` 不支持高级 schema 特性而降级到 `outlines`。
- 结论：当前 vLLM 0.8.5.post1 + Qwen32B BF16 + n-gram speculative decoding 与平台完整审查链路的第二阶段 guided JSON 格式化不稳定。即使调高 `prompt_lookup_min`、降低 `num_speculative_tokens`，仍不能满足可用性要求。

G1 最优档：

- 选择 A：关闭 n-gram。
- 保留：BF16/auto KV Cache、prefix cache、`max_model_len=8192`、`max_num_seqs=1`、`max_num_batched_tokens=8192`。
- 后续 G2 从该稳定档继续做单一变量实验。
- 若未来要重新引入 n-gram，建议改成机制性分流：只让第一阶段自由 JSONL 候选扫描使用 n-gram，第二阶段 guided JSON 格式化固定走无 n-gram 的稳定模型节点；不能在同一个 vLLM 节点上全链路开启。

### G2-G5

### G2: 输出 token 预算

| 档位 | 文件 | 任务 ID | 耗时 | findings/groups | 关键命中 | 明显误报 | 行号准确性 | 格式稳定性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | T1 | `151c0731-5ae0-4c28-a642-9cd7e9a6e315` | 102.6s | 9/8 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `506/516` DES 固定块、`700/706` 相邻循环写入偏重复 | 关键行 `659` 准确；新增 `700/706` 有同根因重复倾向 | 无格式失败；format 51.9s | 未提速，T1 重复和候选噪声略增 |
| A | T2 | `c17ca654-01c0-404b-86ff-50dc4934debd` | 71.3s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 与 G1-A 基本一致 | 无格式失败；format 33.7s | 与基线持平 |
| B | T1 | `a53c185b-cdf6-4416-8561-851159775207` | 100.9s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `397/398` 固定发送缓冲区仍偏可疑；`262/333/453` 需复核容量 | 行号整体准确，根因行 `659` 正确 | 无格式失败；format 49.8s | 当前基线，暂优于 A |
| B | T2 | `63255822-68ac-4338-90c8-6b05f42fb1e8` | 71.4s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.6s | 当前基线，暂优于 A |
| C | T1 | `96ccc81c-1f35-4bef-9edb-3133c0a82681` | 103.0s | 10/8 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | 新增 `506/516` DES 固定块、`700/706` 相邻循环写入、`304` 中危整数计算候选，噪声增加 | 关键行 `659` 准确；新增候选存在相邻重复 | 无格式失败；format 51.5s | 召回增强但噪声更大，暂不优于 B |
| C | T2 | `55643a37-0b4f-4ce2-b415-4394a4472981` | 161.5s | 10/8 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；但字符串拼接候选扩张明显 | 行号可用，但 `446/474/506` 相近根因增多 | 无格式失败；format 81.4s | 候选从 21 增至 52，耗时翻倍以上，淘汰 |

G2-A 详细记录：

- T1 pipeline：`discovered=37; formatted=37; backend_rejected=26; proven_safe_buffer=25; final=9; timing_finalize_total_s=52.2199`
- T2 pipeline：`discovered=21; formatted=21; duplicate_roots=9; backend_rejected=3; final=9; timing_finalize_total_s=33.7472`
- 结论：`1536/1536` 没有降低格式化耗时，反而让 T1 最终结果从 8 条增到 9 条，并出现 `700/706` 同根因重复倾向；暂不作为最优档。

G2-C 详细记录：

- T1 pipeline：`discovered=38; formatted=38; backend_rejected=26; proven_safe_buffer=25; final=10; timing_finalize_total_s=51.7342`
- T2 pipeline：`discovered=52; formatted=52; backend_rejected=33; proven_safe_buffer=31; checked_null_result=2; final=10; timing_finalize_total_s=81.6975`
- 结论：`3072/2048` 确实扩大第一阶段候选，但主要扩大的是已证明安全候选和相近字符串拼接候选；T2 总耗时从 71.4s 增至 161.5s，不适合作为默认。

G2 最优档：

- 选择 B：`CANDIDATE_MODEL_MAX_TOKENS=2048, MODEL_MAX_TOKENS=2048`。
- 理由：关键漏洞稳定命中，`strstr`/`Err_Type` 误报未回潮，候选数量、后处理耗时和重复程度均优于 A/C。
- 后续 G3 从该稳定档继续做单一变量实验。

### G3: 候选动态 token 估算

| 档位 | 文件 | 任务 ID | 耗时 | findings/groups | 关键命中 | 明显误报 | 行号准确性 | 格式稳定性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | T1 | `395a9421-1d67-4d2b-8dfb-234163c25764` | 95.8s | 9/7 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | 新增 `164/304` 中危整数候选，噪声略增 | 关键行 `659` 准确；`164` 根因解释较弱 | 无格式失败；format 44.4s | 略提速，但噪声增加 |
| A | T2 | `10e2bd63-2564-40a3-bb51-d347c5513b61` | 71.7s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 与基线一致 | 无格式失败；format 33.6s | 与基线持平 |
| B | T1 | `a53c185b-cdf6-4416-8561-851159775207` | 100.9s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `397/398` 固定发送缓冲区仍偏可疑；`262/333/453` 需复核容量 | 行号整体准确，根因行 `659` 正确 | 无格式失败；format 49.8s | 最稳，暂选 |
| B | T2 | `63255822-68ac-4338-90c8-6b05f42fb1e8` | 71.4s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.6s | 最稳，暂选 |
| C | T1 | `26d1a4ef-834c-462b-a9f8-0ee6454cf5d3` | 97.1s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `304` 中危整数候选替代了基线 `262` 候选，存在结果漂移 | 关键行 `659` 准确 | 无格式失败；format 45.8s | 略提速但稳定性不如 B |
| C | T2 | `b47029f9-9bc1-4549-afce-c9e88ed989e8` | 70.8s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 与基线基本一致 | 无格式失败；format 33.2s | 与基线接近 |

G3-A 详细记录：

- T1 pipeline：`discovered=31; formatted=27; backend_rejected=17; proven_safe_buffer=16; final=9; timing_finalize_total_s=44.6007`
- T2 pipeline：`discovered=21; formatted=21; backend_rejected=3; proven_safe_buffer=3; final=9; timing_finalize_total_s=33.6898`

G3-C 详细记录：

- T1 pipeline：`discovered=31; formatted=28; backend_rejected=18; proven_safe_buffer=17; final=8; timing_finalize_total_s=45.9959`
- T2 pipeline：`discovered=21; formatted=21; backend_rejected=3; proven_safe_buffer=3; final=9; timing_finalize_total_s=33.2879`

G3 最优档：

- 选择 B：`tokens_per_line=4.5, dangerous=24, pointer=4`。
- 理由：A/C 虽有轻微耗时下降，但 T1 都引入了中危整数候选漂移；B 对关键漏洞、重复率和报告形态最稳定。
- 后续 G4 从该稳定档继续做单一变量实验。

### G4: RAG 上下文量

| 档位 | 文件 | 任务 ID | 耗时 | findings/groups | 关键命中 | 明显误报 | 行号准确性 | 格式稳定性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | T1 | `850e1cd9-48c7-4232-ae59-fb228e6d4e4b` | 101.3s | 10/8 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `506/516/700/706/304` 扩张，疑似上下文不足导致安全约束变弱 | 关键行 `659` 准确；相邻重复增加 | 无格式失败；format 50.5s | RAG 过少，淘汰 |
| A | T2 | `dac759e8-f1b9-43e1-9d50-8e90e3c37562` | 73.5s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.6s | 与基线接近 |
| B | T1 | `a53c185b-cdf6-4416-8561-851159775207` | 100.9s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `397/398` 固定发送缓冲区仍偏可疑；`262/333/453` 需复核容量 | 行号整体准确，根因行 `659` 正确 | 无格式失败；format 49.8s | 最稳，暂选 |
| B | T2 | `63255822-68ac-4338-90c8-6b05f42fb1e8` | 71.4s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.6s | 最稳，暂选 |
| C | T1 | `9dad6f17-d3cd-45fe-815a-e467edfacdf0` | 97.3s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `304` 中危整数候选替代基线 `262`，存在轻微漂移 | 关键行 `659` 准确 | 无格式失败；format 45.6s | T1 可接受 |
| C | T2 | `f6d60e89-f4b9-4e01-ab73-65ebfd72a645` | 162.8s | 10/8 | 命中文件接收/字符串拼接类风险 | 候选从 21 增至 52，`446/474/506` 相近字符串拼接候选增多 | 行号可用但重复倾向增强 | 无格式失败；format 82.5s | RAG 过多，淘汰 |

G4-A 详细记录：

- T1 pipeline：`discovered=37; formatted=37; backend_rejected=25; proven_safe_buffer=24; final=10; timing_finalize_total_s=50.7555`
- T2 pipeline：`discovered=21; formatted=21; backend_rejected=3; proven_safe_buffer=3; final=9; timing_finalize_total_s=33.6439`

G4-C 详细记录：

- T1 pipeline：`discovered=31; formatted=28; backend_rejected=18; proven_safe_buffer=17; final=8; timing_finalize_total_s=45.5554`
- T2 pipeline：`discovered=52; formatted=52; backend_rejected=33; proven_safe_buffer=31; checked_null_result=2; final=10; timing_finalize_total_s=82.5375`

G4 最优档：

- 选择 B：`RAG_KEYWORD_TOP_K=8, RAG_CONTEXT_MAX_CHARS=3000`。
- 理由：A 对复杂协议文件上下文不足，C 对无线模块文件引入明显候选膨胀和耗时翻倍；B 在两类文件之间最均衡。
- 后续 G5 从该稳定档继续做单一变量实验。

### G5

### G5: 格式化批大小

| 档位 | 文件 | 任务 ID | 耗时 | findings/groups | 关键命中 | 明显误报 | 行号准确性 | 格式稳定性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | T1 | `aaf3f5f6-79e8-49c7-be01-487e58e2aa5e` | 106.1s | 11/9 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `506/516/700/706/304/1077` 扩张明显 | 关键行 `659` 准确；中危逻辑候选根因较弱 | 无格式失败；format 52.8s | 小批量没有更稳，淘汰 |
| A | T2 | `9ca5d6b1-086f-4599-8d5e-130e17547180` | 71.2s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.4s | 与基线接近 |
| B | T1 | `a53c185b-cdf6-4416-8561-851159775207` | 100.9s | 8/6 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | `397/398` 固定发送缓冲区仍偏可疑；`262/333/453` 需复核容量 | 行号整体准确，根因行 `659` 正确 | 无格式失败；format 49.8s | 最稳，选择 |
| B | T2 | `63255822-68ac-4338-90c8-6b05f42fb1e8` | 71.4s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.6s | 最稳，选择 |
| C | T1 | `063902b3-47ac-4107-a7d3-85d419717c49` | 98.3s | 9/7 | 命中 `ServiceProtocolRecv` `uMessageLen` 风险 | 新增 `304/1077` 中危候选，结果漂移 | 关键行 `659` 准确 | 无格式失败；format 46.8s | 略快但噪声增加，淘汰 |
| C | T2 | `4c000c26-8edc-4b79-951f-7c0c31ea9e40` | 71.5s | 9/7 | 命中文件接收/字符串拼接类风险 | 未见 `strstr` 判空误报；未见 `Err_Type` 高级误报；`112` 仍为中危建议类 | 行号可用 | 无格式失败；format 33.5s | 与基线接近 |

G5-A 详细记录：

- T1 pipeline：`discovered=38; formatted=38; backend_rejected=25; proven_safe_buffer=25; final=11; timing_finalize_total_s=52.7504`
- T2 pipeline：`discovered=21; formatted=21; backend_rejected=3; proven_safe_buffer=3; final=9; timing_finalize_total_s=33.4278`

G5-C 详细记录：

- T1 pipeline：`discovered=31; formatted=28; backend_rejected=17; proven_safe_buffer=17; final=9; timing_finalize_total_s=46.7695`
- T2 pipeline：`discovered=21; formatted=21; backend_rejected=3; proven_safe_buffer=3; final=9; timing_finalize_total_s=33.5142`

G5 最优档：

- 选择 B：`CANDIDATE_FORMAT_BATCH_SIZE=10`。
- 理由：A 小批量增加请求次数且 T1 噪声明显扩张；C 略快但新增中危逻辑/整数候选，报告形态不如 B 稳定。

## 最终推荐配置

- vLLM：关闭 n-gram speculative decoding。
- KV Cache：保持 `auto`，在当前 BF16 模型路径下实测为稳定路线。
- Prefix cache：开启。
- `MODEL_MAX_TOKENS=2048`
- `CANDIDATE_MODEL_MAX_TOKENS=2048`
- `CANDIDATE_DYNAMIC_TOKENS_PER_LINE=4.5`
- `CANDIDATE_DYNAMIC_TOKENS_PER_DANGEROUS_OP=24`
- `CANDIDATE_DYNAMIC_TOKENS_PER_POINTER_OP=4`
- `RAG_KEYWORD_TOP_K=8`
- `RAG_CONTEXT_MAX_CHARS=3000`
- `CANDIDATE_FORMAT_BATCH_SIZE=10`
- 实验期间 `RAG_SUPPLEMENTAL_PERSIST_ENABLED=false`，用于避免知识图谱 supplemental 写入影响耗时对比；正式长期使用可按是否需要历史图谱增强再开启。

## 总结

- 效率上，n-gram 在当前 vLLM 版本会让完整工作流退回 V0，并在第二阶段 guided JSON 格式化链路出现长时间 95% 停留；不适合作为全链路默认。
- 准确度上，增大候选输出预算、缩小/扩大 RAG、调整动态 token、改变格式批大小都没有稳定提升关键召回；多数变化只增加了候选漂移、重复或可证明安全候选。
- 抗过拟合上，默认的中等 RAG 上下文量和中等格式批大小最平衡；过少上下文会削弱安全证明，过多上下文会带来相似代码干扰。
- 当前最优组合仍接近满血 BF16 模型部署后的稳定基线：少动模型链路，优先依赖后处理的证据约束、低级问题降级和报告分组展示。
