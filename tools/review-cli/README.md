# ccheck-review：代码审查命令行（0.2.1）

独立、无第三方运行时依赖的 Python 客户端，通过 HTTP API 登录、上传目录、查询任务、下载报告、监控 GPU。不改变后端模型、提示词、队列、并发策略或知识图谱生成规则。CLI 提交的任务和 Web 任务进入相同后台流程，可在同一账号的 Web 页面看到。0.2.0 增加动态进度条和后台文件扫描计数；准确文件计数需配套升级后端，见第 9 节。

## 1. 安装前提

- 运行 CLI 的机器有 Python 3.10 或以上版本，能访问已启动的 C-Check Web 服务。
- 准备已有的平台账号；不会自动创建管理员，也不会读取部署环境中的管理员密码。
- CLI 无需 GPU、Docker、PyTorch、vLLM 或模型权重，也不需要安装后端 Python 依赖。
- `--dir` 指向**执行命令机器上的目录**，不是服务端路径。可以在 Linux 部署服务器上执行，也可以从可访问服务的个人电脑提交。
- 服务地址用 Web 服务根地址，例如 `http://127.0.0.1:8800`，不要使用 `/workspace` 页面地址或模型推理端口。也接受以 `/api` 结尾的地址。
- 需要当前服务端支持对应 API；TXT 文件支持取决于服务端版本。CLI 不绕过服务端上传大小、文件数量、账号权限等限制。

## 2. 安装方式

以下命令在项目根目录执行。优先采用 A，不修改系统 Python 环境，也不需要 root。

### A. 单文件方式，无需 pip 或联网安装依赖

```bash
python3 tools/review-cli/build_zipapp.py --output dist/ccheck-review.pyz
mkdir -p "$HOME/.local/bin"
install -m 755 dist/ccheck-review.pyz "$HOME/.local/bin/ccheck-review"
export PATH="$HOME/.local/bin:$PATH"
ccheck-review --version
ccheck-review --help
```

也支持独立的 `help` 命令，无需登录、联网或有效的本机配置：

```bash
ccheck-review help          # 总帮助
ccheck-review help submit   # 提交任务参数
ccheck-review help status   # 状态和进度参数
ccheck-review help gpu      # GPU 监控参数
```

`ccheck-review --help` 和 `ccheck-review submit --help` 等原有形式保持可用。帮助始终输出人类可读文本；未知子命令返回退出码 2。

需要时将 PATH 设置加入自己的 shell 配置。这个 `.pyz` 是普通 Python zipapp，不是签名 RC 安装包；不需要替换现有 RC、重启 Docker、迁移数据库或重新下载模型。复制后也可直接运行：

```bash
python3 /path/to/ccheck-review.pyz --help
```

### B. pipx 或专用虚拟环境

```bash
pipx install ./tools/review-cli
```

或：

```bash
python3 -m venv "$HOME/.venvs/ccheck-review"
"$HOME/.venvs/ccheck-review/bin/python" -m pip install ./tools/review-cli
"$HOME/.venvs/ccheck-review/bin/ccheck-review" --help
```

此方式构建时需要 setuptools；运行时没有第三方依赖。不要用 `sudo pip install` 修改共享服务器系统 Python。

## 3. 登录和本机配置

```bash
ccheck-review login --server http://127.0.0.1:8800 --username YOUR_USERNAME
```

交互输入密码，不回显。登录成功后保存当前服务器和其访问令牌，不保存密码。

默认配置位置：`~/.config/ccheck-review/config.json`；支持 `XDG_CONFIG_HOME`。可通过 `--config /path/config.json` 或 `CCHECK_REVIEW_CONFIG` 改变位置。Linux 上新配置目录使用 0700、令牌文件使用 0600；已有文件若向组/其他用户开放权限，会拒绝读取。Windows 需要用户自行确保个人目录 ACL 不开放给他人。

配置结构如下，通常不必手改：

```json
{
  "server": "http://127.0.0.1:8800/api",
  "profiles": {
    "http://127.0.0.1:8800/api": {
      "token": "ACCESS_TOKEN",
      "allow_insecure_http": false
    }
  }
}
```

服务器优先级：`--server` > `CCHECK_REVIEW_SERVER` > 本机配置 > `http://127.0.0.1:8800`。这里的本机配置指本 CLI 配置，不会自动解析原部署 YAML 或 `.env`，首次登录指定实际端口即可。

令牌优先级：`CCHECK_REVIEW_TOKEN` > 对应服务器的已保存令牌。已保存令牌严格按服务器区分，不会因 `--server` 改变而自动发送给另一台服务器。显式环境变量令牌则由部署者保证与选定服务器对应。

非回环 HTTP 会明文传输账号令牌和源代码，默认拒绝。优先使用 HTTPS 或 SSH 本地转发。在受控内网确需 HTTP 时显式接受风险：

```bash
ccheck-review login --server http://192.168.1.20:8800 \
  --username YOUR_USERNAME --allow-insecure-http
```

成功登录后，该服务器的 HTTP 选择会记录到配置。HTTPS 始终校验证书；CLI 不提供跳过证书验证选项，不跟随 HTTP 重定向。不要将密码、配置文件、访问令牌提交 Git。CLI 会遵循 Python 的 HTTP(S) 代理环境设置，内网部署需检查 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`，防止代码经过不可信代理。

自动化可由秘密管理器注入 `CCHECK_REVIEW_TOKEN`，或向 `login --password-stdin` 提供单行密码。不要把密码明文写入命令历史。令牌过期后重新登录，不会静默保存密码或自动重新登录。

```bash
ccheck-review logout
```

logout 仅移除本机对应服务器令牌，不撤销其他客户端会话，也不会清除 shell 中的环境变量令牌。

## 4. 提交目录

```bash
ccheck-review submit \
  --name WirelessTest20261026105530 \
  --dir /home/lyh/c-check-test-data/E5_Wireless_txt
```

默认提交成功立即返回任务 ID，不等待审查完成。

| 参数 | 默认行为 |
| --- | --- |
| `--name` | 必填，1～128 字符 |
| `--dir` | 必填，递归读取本地目录 |
| `--server` | 本机配置的服务地址 |
| `--model` | 使用服务端当前可见、已启用的默认模型节点 |
| `--checks` | 全部六类，多个类别用英文逗号分隔 |
| `--wait` | 不指定则提交后返回 |
| `--gpu` | 等待任务时附带 GPU／显存仪表；默认不额外查询 GPU，需管理员账号 |
| `--json` | 不指定则简短文本 |
| `--poll-interval` | 等待时每 5 秒查询一次 |
| `--wait-timeout` | 等待总时限；默认无限等待 |
| `--timeout` | 单次 HTTP 超时 60 秒，大目录上传或 PDF 渲染可适当提高 |

递归收集 `.c`、`.h`、`_c.txt`、`_h.txt`，忽略其他文件；保留相对目录和原始字节，由后端负责编码解析及 TXT 文件名还原。不会遵循 `.gitignore`，因此目录中符合扩展名的生成文件和隐藏文件也会上传，请选好输入目录。

目录符号链接不遍历，源文件符号链接和特殊文件拒绝上传。`a.c` 与 `a_c.txt` 等还原后重名会在提交前报错。输入目录应在上传期间保持不变。所有选中文件打成临时 ZIP，走现有 `/api/reviews/archive` 接口；Web 的输入类型会显示 archive，而非 folder，但后续审查处理相同。

ZIP 和 multipart 临时文件用磁盘缓存，不把整个目录装入客户端内存；关闭时自动清理。临时目录需要能容纳 ZIP 及 multipart 副本，敏感代码应使用受控本地临时目录，可通过 `TMPDIR`（Linux）配置。服务端仍有 ZIP 大小、解压总量、单文件大小、文件数等限制，CLI 不自动拆分任务或调整限制；收到 413/422 时检查部署配置。

查询模型和检查类别：

```bash
ccheck-review models
ccheck-review checks
ccheck-review submit --name FocusedReview --dir ./sources \
  --model MODEL_NODE_ID --checks buffer_overflow,resource_leak,integer_safety
```

`--model` 是平台模型节点 ID，不是 Hugging Face 模型名称。未找到唯一默认节点时会报错，不会悄悄换用其他模型。后台原有同模型节点调度保持不变。

| 检查键 | 类别 |
| --- | --- |
| `memory_safety` | 释放后使用／内存破坏 |
| `buffer_overflow` | 缓冲区／数组越界 |
| `pointer_safety` | 野指针／悬空指针 |
| `resource_leak` | 资源泄漏 |
| `integer_safety` | 长度／索引整数风险 |
| `logic` | 严重状态机／协议逻辑 |

## 5. 状态、等待、近期任务

```bash
ccheck-review status TASK_ID
ccheck-review status TASK_ID --wait --poll-interval 5 --wait-timeout 3600
ccheck-review submit --name WirelessTest --dir ./sources --wait
ccheck-review list --name WirelessTest --limit 20 --offset 0
```

交互终端等待时原地刷新进度条，不不断刷屏。文本展示任务 ID、名称、状态、进度、文件数、问题数和排队位置；JSON 返回服务端完整任务状态。等待信息输出到 stderr，最终结果输出到 stdout。`--json --wait` 的 stderr 是逐行 JSON 进度事件，stdout 仍只有最终的一份 JSON。

```bash
ccheck-review status TASK_ID --wait --gpu --poll-interval 2
```

显示示例（演示数据）：

```text
TASK_ID  WirelessTest
Review [############------------]  50.0% running
scan_files=6/10 remaining=4  uploaded=10
findings=8  queued_ahead=N/A
Server GPUs (shared, NOT task-exclusive); sampled 2026-10-26T10:00:00Z
GPU 0 NVIDIA RTX 5880 Ada Generation
  VRAM [##################------]  75.0% 36.0/48.0 GiB
  GPU  [#####################---]  90.0%
```

- 进度百分比来自服务端，不是客户端时间估算。刷新频率是轮询间隔加请求耗时，并非服务端推送。
- `scan_files` 是**当前这轮第一阶段扫描计划**中的文件完成数／总数；同一文件可能含多个函数单元和分片，全部计划分片成功后才计完成，支持并发乱序完成。
- `remaining` 包括尚未开始和扫描中的文件。失败分片不计完成；上下文过长重新分片、整轮重试时会重置计数，不能据此判定任务倒退。
- 启用 RAG 函数单元计划时，计划中的唯一文件数可能少于上传数；`uploaded` 单独显示上传文件总数。计数表示调度计划的执行进度，不是“每行代码均已覆盖”或“无漏报”的保证。
- `remaining=0` 不代表报告已经完成，第二阶段处理和报告生成可能仍在继续。以任务 `completed` 为准。
- 老服务端、历史任务、尚未生成扫描计划时显示 `N/A`，不通过百分比反推文件数，也不会补造历史统计。
- 重定向、非 TTY、`TERM=dumb` 时降级为无 ANSI 的逐行日志。`--json` 始终输出机器可读 JSON，不插入进度条控制符。终端过小时会截短长行、限制展示行数，可用 JSON 查看完整数据。

Ctrl+C、等待超时或客户端网络故障**不取消后台任务**。可通过 task ID 继续查询。如果提交后响应丢失，CLI 不会自动重试创建任务，避免重复审查；用 `list --name NAME` 与 Web 核实后再决定是否重提。名称不是幂等键，也不保证唯一。

## 6. 下载报告

```bash
ccheck-review report TASK_ID --output ./report.pdf
ccheck-review report TASK_ID --output ./report.md
ccheck-review report TASK_ID --output ./report.txt
ccheck-review report TASK_ID --output ./report.json
ccheck-review report TASK_ID --json
```

输入的是**任务 ID**，CLI 自动查询对应 report_id。扩展名选择格式，可用 `--format pdf|markdown|text|json` 覆盖。PDF/Markdown/文本直接调用服务端既有渲染接口，JSON 获取服务端结构化报告。

未指定输出路径时，默认将 Markdown 输出到终端；配合 `--json` 则输出结构化报告。PDF 必须指定输出路径。输出父目录需提前存在；已有文件不覆盖，除非显式指定 `--force`。下载先写同目录临时文件，成功后再发布，HTTP 错误、非 PDF 内容或可检测的截断不会替换已有文件。报告尚未生成、服务端 PDF 渲染失败时会返回错误，不伪造本地报告。

## 7. 脚本集成与退出码

```bash
ccheck-review submit --name WirelessCI --dir ./sources --json > task.json
TASK_ID=$(python3 -c 'import json; print(json.load(open("task.json"))["id"])')
ccheck-review status "$TASK_ID" --wait --json > status.json 2> progress.jsonl
ccheck-review report "$TASK_ID" --output ./report.json --json
```

实际 CI 需检查每条命令退出码，再执行后续命令。错误输出在 stderr，`--json` 时包含 `error`、`exit_code`，必要时附 `task_id` 或 `task_name`；不打印登录密码、令牌、完整 HTTP 错误体。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功；审查中或审查发现问题本身不表示 CLI 失败 |
| 1 | 网络、HTTP、报告未就绪或其他运行错误 |
| 2 | 参数、本机配置、目录或模型选择错误 |
| 3 | 未登录、令牌失效或无权限 |
| 4 | 查询／等待发现任务失败 |
| 5 | 等待超时，后台继续执行 |
| 130 | 用户中断，后台继续执行 |

若 CI 需要“发现高危则失败”，应读取 JSON 报告中的严重度计数单独设置门禁；CLI 不把“有问题”和“任务执行失败”混为一谈。

## 8. 开发验证

```bash
python3 -m unittest discover -s tools/review-cli/tests -v
python3 tools/review-cli/build_zipapp.py --output dist/ccheck-review.pyz
python3 dist/ccheck-review.pyz --help
```

测试通过本机临时 HTTP 服务覆盖认证、multipart/ZIP 字节、模型选择、轮询、JSON、报告下载与错误分支，不访问部署服务器，不启动模型推理。另有 `backend/tests/test_review_cli.py` 用真实 FastAPI 路由与临时数据库验证 API 契约，禁用 Worker 分发。已有 RC 发布包及部署 CLI 均保持不变。

## 9. GPU 监控与升级边界

当前共 **10 条子命令**：`login`、`logout`、`submit`、`status`、`list`、`report`、`models`、`checks`、`gpu`、`help`。

单独查看 GPU：

```bash
ccheck-review gpu
ccheck-review gpu --watch --poll-interval 2
ccheck-review gpu --watch --samples 10 --poll-interval 2 --json
```

`--watch` 默认持续刷新，Ctrl+C 退出；`--samples` 可限制采样次数。JSON 模式下采样事件写入 stderr，有界采样结束后最后一份快照写入 stdout。

`VRAM` 显示显存占用比例及已用／总量（GiB）；`GPU` 显示 GPU 运算利用率。二者不是同一指标，**显存利用率条也不是模型检查完成进度**。显示每块卡的数据及服务端采样时间，不将所有 GPU 简单平均为任务进度。

数据来自 API 服务能够采集的 GPU，属于服务器级共享资源，可能包含 Jina、其他用户和其他任务的显存，不是本任务独占占用；远程推理节点若未被该服务采集，不会凭空显示出来。沿用后台管理员权限，普通账号不会被自动提权。

GPU 无权限、采集异常、未暴露设备或数据缺失时显示 unavailable／N/A，不伪装成 0%。附带 `--gpu` 的任务查询不会因为 GPU 监控失败而取消任务。永久的 401/403/404 不会每次反复查询。新后端优先使用轻量 `/api/admin/resources/gpus` 接口，只采集 GPU，避免频繁轮询所有模型的吞吐指标；旧后端 404 时回退既有 `/api/admin/resources` 接口，旧接口可能更慢。

升级边界：

1. **只更新 CLI**：无需数据库迁移、Docker 重启或模型重下载。已有服务可显示总进度条、通过原有管理员资源接口显示 GPU 数据；没有新统计字段时文件数显示 N/A。
2. **需要准确文件计数**：必须一起部署本次后端代码、API 和 Worker，并执行 Alembic `0017_review_scan_progress` 迁移，新增可空的 `scan_file_total`、`scan_file_completed` 两列。API 同时返回计算字段 `scan_file_remaining`。历史数据保持原样，不回填估算值。
3. 源码部署应在本项目维护窗口备份数据库，停止接收新任务并等待在途任务完成，再通过项目既有迁移流程执行 `alembic upgrade head`，最后启动配套版本 API/Worker。不要仅升级 API 留下旧 Worker，否则新任务仍无文件统计。
4. 签名 RC 部署需要将后端代码、迁移和相应版本清单一起纳入下一个正式签名补丁，**不要直接修改已签名 rc.18 文件或自行改写其哈希**。本次本地 CLI 构建不是新的 RC 发布，也尚未对线上执行迁移。
5. 回滚应先停止本项目写入、切换旧后端，然后按既有流程回退迁移。0017 的回退只删除两项新统计列，但仍应在维护窗口和有备份的前提下操作。

后端回归入口：`backend/tests/test_scan_progress.py` 覆盖乱序完成、同文件多分片、重新分片重置、失败不计完成、计数落库、旧记录迁移与 GPU 管理员权限；不调用真实模型或 GPU。
