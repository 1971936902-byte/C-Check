# Linux 编译打包环境恢复与发布手册

更新：2026-10-22。面向发布者/构建机，不是接收者的一键安装手册。

## 1. 已保存什么

当前仓库的 `deploy/release/` 是完整发布工具链：

| 资源 | 作用 |
| --- | --- |
| `publisher.py`、`publisher.example.json` | 新 Linux 构建机的分阶段入口和外置配置模板 |
| `baselines/rc13/locks/` | 已发布 RC13 的 backend、Qwen、embedding、compiler 精确依赖及 wheel 哈希 |
| `baselines/rc13/wheels.json` | 272 条分环境 wheel 记录、大小、哈希和官方来源 |
| `baselines/rc13/base-images.json` | Python、Node、Nginx、MySQL、Redis、Qdrant 的六个镜像摘要 |
| `baselines/rc13/vendor/` | 实际使用的 Nuitka 2.7.12 编译 wheel，约 3.5MB，保留内置许可 |
| `baselines/rc13/models/` | 模型文件版本/哈希清单和说明，不含权重 |
| `baselines/rc13/apt-inventory-*.txt` | 已验证镜像的系统包版本清单，用于比对，不是离线 deb 集合 |
| `baselines/rc13/baseline.json` | 上述输入文件的校验清单及原发布包身份 |
| `pack_source.py` | 导出批准的已提交源码，不混入当前目录的私人文件 |
| `Dockerfile.*`、`compile_core.py`、`assemble_backend.py` | 容器构建、核心编译、最终镜像组装 |
| `build_images.py`、`validate_images.py` | 镜像构建、源码/编译版对照、前端/CUDA/隔离及泄漏检查 |
| `build_release.py`、`build_cli.py`、`INSTALL-RTX5880.template.md` | 二进制包、CLI、许可材料、分片、签名及版本化安装说明组装 |
| `capture_baseline.py` | 从后续已验证构建中提取新的可公开基线 |
| `prepare_build.py` | 需要升级依赖时的高级基线生成工具，不是普通恢复步骤 |
| `upload_github_release.py` | 显式、人工授权的 GitHub 草稿发布工具 |
| `tests/` | 部署与构建工具的单元/契约测试 |

没有提交：SSH 连接信息、密码、GitHub token、签名私钥、公司 C 源码、业务数据库、模型权重、完整 wheel 缓存、旧虚拟环境或 BuildKit 私有编译阶段。

当前构建机和缓存不因本次整理被删除、重建或停止。GitHub 保存的是可迁移工具链和批准输入，不是整台服务器快照。服务器本身仍应由管理员按公司政策做私有备份。

## 2. 能复现到什么程度

普通恢复路径不再依赖 `/opt/c-check/.venv`、`/opt/vllm-qwen` 等旧主机固定目录，也不在新宿主机 pip 安装整套推理环境。宿主 Python 运行标准库工具，真正编译和运行依赖在 Docker 中准备。

保留了精确 Python wheel/哈希、基础镜像摘要和模型版本。缺失的 wheel 从官方 URL 或 PyPI 元数据解析，大小和 SHA256 必须与清单完全一致；无匹配就失败，不自动升级。Nuitka 的本地构建 wheel 不能保证重建后哈希不变，因此保留了实际使用的原始第三方 wheel。

**这不是字节级可重复构建承诺。** 当前 Dockerfile 的 apt 安装仍使用在线仓库，系统包可能漂移；清单是比对证据，并未提供完整 Debian snapshot/deb 仓库。npm 包来源、编译输出和时间戳也可能影响镜像结果。上游删除历史镜像或文件时需使用管理员保留的缓存，不能改哈希跳过。

任何新源码、新版本、新依赖组合都必须重新跑完整 gates，并在目标机器做真实安装/任务验收。不要复用 RC13 的 gates.json 冒充新版本已经测试，也不要改 RC13 的发布附件或移动已公开标签。

## 3. 新 Linux 构建机前置

- Linux amd64，建议 Ubuntu 22.04/24.04，Python 3.10+、OpenSSL 3+、Git、Docker Engine/BuildKit；用户经批准可管理 rootful Docker。
- 建议 16 逻辑 CPU、64GB RAM、至少 150-200GB 空闲，并给 wheel、Docker 层、临时镜像导出及多版本包留余量。具体峰值受缓存/文件系统影响；不要只按最终 5GB 包大小规划。
- 编译镜像本身不要求加载模型；完整 `gates` 包含 CUDA/非 root GPU 检查，必须有可用 NVIDIA GPU、驱动与 Container Toolkit。目标模型的真实推理验收还需匹配权重和足够显存。
- 此分支不含 BF16 32B 双卡部署配方；保存的是目前单卡 FP8 发布线路。不要仅改模型目录就宣称支持满血/TP 版本。
- 新机需访问 Docker 镜像仓库、Debian/Alpine 软件源、npm 镜像、PyPI 和 PyTorch wheel 源。只复用现有 wheel 缓存不等于整个构建过程完全离线。
- 推荐专用构建机。共享 GPU 机器先协调使用窗口；工具不会替你安装驱动、重启全局 Docker、修改防火墙或杀别人的任务。

宿主检查：

```bash
uname -m
/usr/bin/python3 --version
openssl version
git --version
docker version
docker buildx version
docker compose version
docker info --format '{{.DockerRootDir}}'
nvidia-smi
free -h
df -h
```

如果只能 sudo 使用 Docker，就在整个构建流程中统一使用经授权的 sudo，而不是一部分以普通用户执行、一部分以 root 执行。不要把 Docker socket 改成全员可写。

宿主安装和 Toolkit 注意事项见仓库 `docs/RTX5880_48GB_一键部署前置准备与安装运维手册_v0.1.0-rc.13.md`。该文档中接收者路径/18800 端口不是构建机必须使用的路径。

## 4. 获取代码并选择新版本

```bash
git clone --branch codex/publisher-toolchain --single-branch \
  https://github.com/1971936902-byte/C-Check.git
cd C-Check
git status --short
git rev-parse HEAD
/usr/bin/python3 -m unittest discover -s deploy/release/tests -v
```

下载与构建须使用经公司批准的源码提交。审查完成后把 `source_ref` 改成具体 commit SHA，优于一直使用会移动的 HEAD。脚本只导出提交内容，尚未提交的本地修改不会被偷偷带入安装包。

复制配置到源码目录之外，避免公开个人路径/配置或把产物嵌套打进源码：

```bash
mkdir -p "$HOME/c-check-publisher"
cp deploy/release/publisher.example.json "$HOME/c-check-publisher/publisher.json"
chmod 600 "$HOME/c-check-publisher/publisher.json"
```

示例内容：

```json
{
  "version": "v0.1.0-rc.14",
  "source_ref": "HEAD",
  "work_root": "/srv/c-check-builds/v0.1.0-rc.14",
  "baseline": "rc13",
  "wheel_cache": null,
  "wheel_download_policy": "online"
}
```

按权限调整 `work_root`，例如 `$HOME` 实际展开后的 `/home/builder/c-check-builds/v0.1.0-rc.14`，JSON 中不要使用 `$HOME` 变量。目录必须是新的专用绝对路径，在 Git checkout 之外，不能指向主目录本身、其他项目或符号链接。

版本号只是新构建的示例，不表示 RC14 已发布。先查 GitHub 是否已有该版本，已存在则换一个未使用的版本。`baseline=rc13` 表示保留 RC13 的依赖/模型输入，不表示产物版本仍是 RC13，也不表示新产物已有实机验证。

配置在工作目录初始化后不能随意变更：需要换版本、source_ref 或缓存策略时用新的 work_root。后续阶段使用首次冻结的源码，而不是重新读取移动后的 HEAD。

## 5. 分阶段执行

建议使用 tmux 或管理员批准的 systemd 作业运行长步骤。下面命令在代码仓库根目录执行，示例变量每次新开终端需要重新设置：

```bash
CONFIG="$HOME/c-check-publisher/publisher.json"
```

### 5.1 准备源码、依赖和模型元数据

```bash
/usr/bin/python3 deploy/release/publisher.py prepare --config "$CONFIG"
```

此步骤会导出提交、验证基线、下载/复用精确 wheel，准备 locks、模型元数据及纯 Python PyYAML。首次需下载数 GB 的 wheel，不下载 32B/Jina 权重，不在宿主 Conda 中安装库。

可复用已保留的缓存：把外置配置 `wheel_cache` 指向某个旧构建的 `source/wheelhouse`，结构必须保留 backend/qwen/embedding/compiler 子目录。缓存只作为读取来源，逐文件验 hash；不要在运行中的构建里原地修改缓存。

`wheel_download_policy=offline` 禁止补下载 wheel，缺一个就失败，不会自动切 online。这一设置不控制后续 Docker pull、apt 或 npm；完全离线构建还需要单独保存基础镜像和内部系统包/npm 源。

成功结束输出 `COMPLETED prepare ...`。中断后确认原进程已经退出，再运行同配置的 prepare 复用已校验文件。若在源码导出/解包阶段中断，可能需要管理员先隔离保留该未完成目录并使用新 work_root，不能对不明目录强制覆盖。

### 5.2 拉取固定摘要的基础镜像

```bash
/usr/bin/python3 deploy/release/publisher.py bases --config "$CONFIG"
```

按 digest 拉取六个基础/数据服务镜像，不把共享主机的 `python:latest` 等可变标签重新指向其他内容。后续构建通过受控的 `CCHECK_BASE_IMAGE_LOCK` 使用摘要，不依赖主机当时的裸标签。

### 5.3 构建最终镜像

```bash
/usr/bin/python3 deploy/release/publisher.py build --config "$CONFIG"
```

Nuitka、gcc 等编译工具位于编译阶段。最终应用镜像不复制编译阶段源码；不要把 Docker builder target 当运行镜像交付。

一个构建失败后可以只重做相应镜像：

```bash
/usr/bin/python3 deploy/release/publisher.py build --config "$CONFIG" --only backend
```

完整发布前仍必须具有 backend、frontend、qwen、embedding 全部记录。不能只完成 backend 就组装正式包。

### 5.4 完整门禁

```bash
/usr/bin/python3 deploy/release/publisher.py gates --config "$CONFIG"
```

包括源码/编译版测试集合及结果对照、部署工具测试、前端测试、CUDA 与版本检查、非 root 文件可读性和 GPU registry、镜像所有历史层审计、删除后仍残留源码的负例检测。

门禁确实会启动短生命周期测试容器，并使用 GPU；安排空闲窗口。失败就停止，不创建空的 passed 文件、不手改 gates.json。某一步尚在运行时不要重复启动；本工具有工作目录锁，但不能把“进程断线”直接当成“作业已终止”。

### 5.5 签名组装

签名私钥必须由发布负责人保管在工作目录和仓库之外，权限 0600，不提交 Git、不复制进 Docker 上下文。

迁移原发布身份时，管理员必须通过独立安全通道恢复原私钥；GitHub 上的公钥不能还原私钥。如果确实使用全新发布身份，下面仅是新建 Ed25519 密钥示例，生成后必须通知接收者批准新指纹，不能冒充原指纹：

```bash
mkdir -p "$HOME/c-check-publisher-keys"
chmod 700 "$HOME/c-check-publisher-keys"
(
set -eu
umask 077
KEY="$HOME/c-check-publisher-keys/publisher-private.pem"
test ! -e "$KEY"
openssl genpkey -algorithm ED25519 -out "$KEY"
openssl pkey -in "$KEY" -pubout -out "$HOME/c-check-publisher-keys/publisher.pem"
sha256sum "$HOME/c-check-publisher-keys/publisher.pem"
)
```

有获批私钥且完整 gates 成功后：

```bash
/usr/bin/python3 deploy/release/publisher.py package --config "$CONFIG" \
  --signing-key "$HOME/c-check-publisher-keys/publisher-private.pem"
```

输出目录是 `work_root/release-版本/`，包含签名清单、CLI、镜像压缩分片、核心 wheel、模型清单、锁及许可。每分片默认 1.5GiB，权重不在包内。组装再次核对 source、version、image IDs 与 gates 一致。

同一输出版本目录存在时拒绝覆盖。组装失败的私有未发布目录要先诊断；已公开版本永远不能覆盖。随包 INSTALL-RTX5880.md 从模板生成，版本和公钥文件 SHA256 来自本次组装，不沿用旧 RC13 值。仓库静态 RC13 手册仍只适用于 RC13；接收者使用新包生成的说明，并通过独立可信渠道批准本次公钥指纹。

## 6. 结果、日志和恢复

```text
work_root/
  publisher-owner.json       本构建配置与所有权
  .publisher.lock            并发锁，不是长期任务状态证据
  operation.json            最后阶段、开始 PID 或完成/失败状态
  source.tar.gz             已提交源码快照，只给受信构建者
  source/                   冻结源码、wheelhouse、locks、镜像构建记录
  tools/yaml/               从已校验 wheel 提取的纯 Python YAML
  prepared.json             准备完成的源身份和 wheel 数量
  logs/                     各步骤日志，只在私有工作目录
  gates/                    本次完整测试结果
  release-版本/             接收者签名安装包
```

查看状态：

```bash
/usr/bin/python3 deploy/release/publisher.py status --config "$CONFIG"
```

按实际 work_root 查看 `logs/*.log`。日志、源码快照和 Docker 编译缓存可能含内部实现，不能整目录上传公开仓库。任务在后台时先核实对应 PID、systemd 单元或 tmux 会话，不因为最后一行日志不动就启动第二份构建。

工具不会主动删除旧构建目录、镜像、缓存或发布包，也不执行全局 prune。空间不足先按项目、所有权及保留政策评估，不自动删除别人资源。

## 7. 安装验收与 GitHub 发布

构建门禁不等于目标机验收。用新包在独立、此前不存在的实例目录安装，按该版本接收者手册验证真实审查任务、行号、图谱/RAG、报告导出、指标和再次安装。GPU 不支持或小任务未通过，就不要宣布适配完成。

发布是独立人工动作，publisher.py 不登录 GitHub、不推送代码，也不自动发布 Release。经授权安装/登录 GitHub CLI 后，先推送与签名 `source.git_sha` 完全一致的标签；不要对当前随便一个 HEAD 打标签。

```bash
# 以下占位值必须替换为实际版本和 release.json 中的源提交
git tag -a release-实际版本 实际源提交SHA -m 'Approved binary release'
git push origin refs/tags/release-实际版本
```

使用实际完整包目录、独立可信公钥和已写好的发布说明，先预览：

```bash
/usr/bin/python3 deploy/release/upload_github_release.py \
  --release /实际work_root/release-实际版本 \
  --trusted-key /独立可信目录/publisher.pem \
  --repository 1971936902-byte/C-Check \
  --notes /发布说明目录/release-notes.md
```

确认用户授权、仓库、源提交/标签、许可、验收结果及资产清单后，追加 `--publish-draft` 才创建草稿并上传。脚本拒绝 worktree snapshot，并通过 GitHub API 核对远端标签真实指向签名源提交；RC 版本标记为 prerelease。

保持草稿，直到所有资产名称、大小与 GitHub 返回的 SHA256 都和本地签名包一致。网络中断可能留下 starter 资产，先确认原上传进程已退出，逐一处理本草稿的未完成文件，不能用 --clobber 覆盖已公开版本。完成校验后再由授权发布者公开草稿，并验证匿名下载/签名。

公开仓库会自动产生标签源码下载链接。二进制发布并不能隐藏这个仓库已有或新增的源码；若未来真正区分私有源码仓库与公开二进制仓库，需要另行设计签名源证明和标签策略，本工具不宣称当前公开仓库已经实现源码保密。

## 8. 保留与迁移构建环境

除 GitHub 工具链外，管理员应在私有存储保留：

- 原签名私钥的加密离线备份、授权人和公钥指纹记录，不与公开资源打包。
- 已验证的完整 wheelhouse，保留环境子目录与哈希；新构建可配置为只读缓存。
- 基础镜像按 digest 的离线副本、Docker/BuildKit/驱动/Toolkit 版本清单。
- 需要完全离线时的 apt snapshot/deb 与 npm 缓存/内部源；当前仓库未提供这些大体积副本。
- 源提交、外置构建配置、完整 gates、真实安装验收记录和最终签名包。
- 必须保留的模型权重和业务数据，按各自许可和公司保密制度存放，不上传 GitHub。

不要直接把旧服务器 `/root`、`/home`、虚拟环境或完整 Docker 编译缓存压缩公开。尤其编译 target 可能含第一方明文源码，只有最终运行镜像经过交付审计。

## 9. 后续升级依赖或建立新基线

普通重建使用本手册的冻结基线。确实要更新依赖时，在隔离候选环境中审查版本，再用高级工具显式指定 Python 路径：

```bash
/usr/bin/python3 deploy/release/prepare_build.py /候选源码上下文 \
  --backend-python /候选backend环境/bin/python \
  --qwen-python /候选qwen环境/bin/python \
  --embedding-python /候选embedding环境/bin/python \
  --compiler-python /候选compiler环境/bin/python \
  --model-root /获批模型缓存
```

此工具从候选环境冻结依赖，模型缓存需要对应 `download-manifest.json` 和完整文件。它不是从一台任意 Python 环境推导正确部署组合的自动解算器，也不自动授予模型许可。

候选版本完成完整构建、门禁、签名和真实验收后，提取可公开的新基线：

```bash
/usr/bin/python3 deploy/release/capture_baseline.py \
  --context /已验证源码上下文 \
  --release /已验证签名包 \
  --trusted-key /独立可信目录/publisher.pem \
  --base-images /本次确实采用的base-images.json \
  --output /私有审核暂存目录/new-baseline
```

该命令会校验发布签名、源身份和 wheel，再读取三个运行镜像的系统包清单；只启动短暂只读、无网络的工具容器，不加载模型。它不导出私钥或模型权重，但输出仍必须人工审查，再放入一个新的 `baselines/名称/`、测试并提交。不要直接改 rc13 基线来让旧哈希通过。

## 10. 迁移成功的标准

不是“git clone 成功”或“镜像 build 成功”即可结束，应分别确认：

1. 工具测试通过，批准输入校验通过，私钥不在仓库和构建上下文。
2. 新机完成 prepare、bases、build、gates、package，日志与源身份对应。
3. 目标机完成独立安装、真实审查、图谱/RAG/导出/监控和运维验收。
4. 若发布，标签、签名源提交、完整资产哈希和匿名下载全部确认。

本次整理保留了 RC13 的实际构建输入，并测试新增恢复/校验逻辑；未把一个尚未在全新 GPU 主机重新跑完的冷构建描述为已经完成的迁移验收。
