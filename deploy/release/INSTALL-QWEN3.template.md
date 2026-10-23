# rc17：RTX 5880 Ada 48GB 安装与模型迁移

版本：`@@VERSION@@`。这是模型与推理环境升级，不是只调整部署参数的 rc16 补丁。
Qwen 改为官方 `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8`，需要约 31.2GB 的新模型文件。
旧 Qwen2.5 文件不能重命名后复用。Jina、1536 维向量索引、审查核心、提示词和历史报告保持原有内容。

新包替换 Qwen 推理镜像，并以旧后端镜像为基础只更新模型注册/验收管理入口；核心二进制逐文件核对原签名哈希。
五个未变化镜像继续从 rc14/rc16 复用。镜像和模型不允许根据浮动的 `latest` 标签下载。
这是传统镜像补丁交付，`prepare-assets` 等方案二专用命令不适用于此包。

## 一、部署前准备

- Linux amd64；目标为 Ubuntu 24.04、单张 RTX 5880 Ada 48GB。
- 管理员预先准备 Docker、Compose v2、NVIDIA Container Toolkit、CDI、系统 Python 3.10+ 和 OpenSSL 3+。
- 宿主机不安装项目 Python 库，不修改 Conda base，不自动升级驱动或重启共享 Docker。
- 确保共享 GPU 无未协调的其他负载；GPU 编号只选一个设备，也可填写完整 GPU UUID。
- 保留旧镜像、旧模型和旧签名包，直至新版本验收及回滚演练完成，不执行 `docker system prune -a`。
- 安装器按实际镜像、模型与缓存预算检查空间。升级时同时保留两套 Qwen 权重，仅模型部分就约 65GB；另需镜像、备份、缓存和数据库空间。
- Qwen3 官方模型为 Apache-2.0；Jina 仍需按其许可证单独审核公司内部使用授权，`license_mode` 不代表获得许可。

只读检查：

```bash
nvidia-smi
sudo docker version
sudo docker compose version
sudo nvidia-ctk cdi list
/usr/bin/python3 --version
openssl version
df -h
```

依赖的精确版本、构建输入哈希见签名包 `locks/qwen.lock` 和 `verify/build-gates.json`。
不能单独在运行容器里升级 vLLM、PyTorch 或 Transformers。
宿主 `nvidia-smi` 显示的 CUDA 版本是驱动支持能力，不是要求容器使用相同版本。
硬件测试范围以 `verify/build-gates.json` 及发布验收记录为准；A100 验证不能冒充 RTX 5880 实机验证。

## 二、下载与信任校验

发行者公钥文件的独立可信 SHA256 为：

```text
@@PUBLISHER_PEM_SHA256@@
```

先从独立可信渠道确认该指纹，再验证 `release.sig`，不要先运行刚下载且尚未验证的安装脚本。
沿用旧版本时应直接使用原来已信任的公钥，而不是用新下载的公钥覆盖它。

```bash
openssl pkeyutl -verify -pubin -inkey /trusted/publisher.pem -rawin \
  -in release.json -sigfile release.sig
```

使用已经验证过的发布 CLI 获取完整的签名包：

```bash
/usr/bin/python3 /trusted/cli/ccheck.pyz fetch \
  --version @@VERSION@@ \
  --base-url https://github.com/1971936902-byte/C-Check/releases/download \
  --destination /home/lyh/c-check-releases \
  --trusted-key /trusted/publisher.pem
```

旧 CLI 的 `fetch` 只负责拉取和校验，实际升级必须使用新包中的 CLI。
`fetch` 会根据签名清单获取新镜像及复用的旧镜像分片。旧的 `bootstrap.tar.gz` 不能替代新 bootstrap。
有带宽限制时，经独立信任校验的 rc17 CLI 支持在 `fetch` 时增加
`--reuse-release /原实例路径/releases/v0.1.0-rc.16`，核验完整旧包后复用新清单明确声明的未变化镜像分片。
旧 rc16 CLI 没有该选项；使用旧 CLI 的普通 `fetch` 仍然可行，但可能重新下载这些归档。
不要提前把文件复制到未建立下载归属记录的目标目录，`fetch` 会拒绝接管这类非空目录。
不同版本分目录保存，不覆盖 rc16；不要把全部旧文件无差别复制到新目录。

```bash
RELEASE=/home/lyh/c-check-releases/release-@@VERSION@@
/usr/bin/python3 "$RELEASE/cli/ccheck.pyz" package-verify \
  --release "$RELEASE" --trusted-key /trusted/publisher.pem
```

下载到的补丁附件并不等于已经重建的完整发布目录。必须先解开已验证的 bootstrap、补齐签名要求的复用分片，
以 `package-verify` 成功为准。模型权重不包含在补丁附件中。

## 三、模型与站点配置

新安装复制包内 `site-rtx5880.example.json` 为自己的外部配置文件，逐项检查：

- `instance`、`data_dir` 必须是本项目专用值，不能指向其他人的目录。
- `model_paths.qwen` 指向新 Qwen3 目录；`model_paths.jina` 仍指向已有 Jina 1.5B 目录。
- 模型全部就绪时使用 `model_download_policy: offline`；允许按清单下载时使用 `online`。
- `gpu_access_mode: cdi`；不自动改宿主默认 runtime。
- 示例网络仍为 `172.30.10.0/26`、`172.30.10.64/26`、`172.30.10.128/26`，先确认不与本机已有网络冲突。
- 默认 Web 仅绑定 `127.0.0.1:18800`。内网访问可选 `lan`，公网由管理员提供 HTTPS 反向代理和 NAT。

示例保留 GPU Jina 选项：Qwen 显存比例 0.82、并发序列 2、调度 Token 预算 8192、片段请求并发 2。
上下文仍为 8192，最大输出 2048，KV Cache 为 auto/BF16，n-gram 关闭。
目标机必须验证两模型共同驻留、长输入与并发请求；不能用“模型能加载”代替这些验收。
已有实例不会因为升级而自动从 CPU Jina 切成 GPU Jina。显式切换使用 `--performance-profile shared-gpu`。
保留 CPU/串行运行时可使用 `--performance-profile cpu-serial`；这不是删除 GPU 支持。

启动时所有模型文件均按签名清单核对，缺失或版本不同不会被当成可用模型。
运行容器设为离线模式，平台不会为了模型补充从公司上传代码到外部模型 API。

## 四、全新安装

```bash
sudo /usr/bin/python3 "$RELEASE/cli/ccheck.pyz" install \
  --config /home/lyh/c-check-site-rc17.json \
  --release "$RELEASE" --trusted-key /trusted/publisher.pem \
  --version @@VERSION@@
```

以命令返回 `READY` 为准。安装器会验证原生核心、解析器、真实模型调用、Jina 语义排序、代码审查、图谱和报告导出。
首次账户为 `admin`，随机初始密码保存在本实例 `secrets/admin_password`，不要把它粘贴到聊天或提交到 GitHub。

## 五、rc16 增量迁移

不要使用新示例的 `data_dir` 覆盖原实例路径。以原外部站点配置为基础，另外保存 rc17 配置，
只更新新模型路径和需要明确调整的下载策略。`review_model` 由目标签名包确定。
准备独立的加密备份密钥，妥善保管；密钥不能放在实例目录或发布目录里。

```bash
umask 077
openssl rand -hex 48 > /home/lyh/private-backup-key
```

若已设置符合安装器要求的十六进制文本备份密钥，直接沿用，不要重复生成覆盖。
密钥文件必须仅当前用户可读写，不能用原始二进制随机文件替代。执行：

```bash
sudo /usr/bin/python3 "$RELEASE/cli/ccheck.pyz" update \
  --config /home/lyh/c-check-site-rc17.json \
  --release "$RELEASE" --trusted-key /trusted/publisher.pem \
  --version @@VERSION@@ --model-migration \
  --backup-key /home/lyh/private-backup-key
```

新模型先下载/核验到独立的 `models/qwen-revisions/<清单SHA256>/`，不会覆盖旧 `models/qwen/`。
模型准备完成后才排空任务、维护暂停、加密备份、切换本项目服务并进行验收。
数据库、上传、历史报告、现有密码和密钥保持原有值；Jina 版本不变，不重建向量索引。
没有 `--model-migration` 时拒绝模型清单变化；Jina/索引维度、数据库 schema 或核心二进制变化也不能走这条迁移路径。

管理员修改过登录密码时，原初始密码文件可能不再匹配验收登录。迁移前先解决验收凭据问题，
不要清库或自动重置账户。失败日志和备份需保留，维护暂停不能在未确认恢复前强行删除。

## 六、验证与回滚

```bash
sudo /usr/bin/python3 "$RELEASE/cli/ccheck.pyz" verify \
  --config /home/lyh/c-check-site-rc17.json --fresh
```

验收还应人工核对真实漏洞和已修复版本，检查原始候选、最终报告、根因、行号及重复情况。
报告数量增加或减少都不能直接证明准确率提升。发布验收记录应区分原始模型与后处理的作用。

需要回退时，使用 rc17 的新 CLI 管理旧签名包，不降级部署管理器：

```bash
sudo /usr/bin/python3 "$RELEASE/cli/ccheck.pyz" rollback \
  --config /home/lyh/c-check-site-rc17.json \
  --release /原实例路径/releases/v0.1.0-rc.16 \
  --trusted-key /trusted/publisher.pem --model-migration \
  --backup-key /home/lyh/private-backup-key
```

回滚仍需完整旧签名包、旧镜像及旧模型；因此不要提前清理它们。
自动恢复失败时保留现场，检查实例 `state/`、容器日志和加密备份，不能删除卷或重装覆盖数据库。
