# RTX 5880 Ada 48GB 安装说明

适用：Linux amd64、Ubuntu 24.04、单张 RTX 5880 Ada 48GB，已有本地模型。
这是预发布适配包；A100 和 RTX 4090 Ada 48GB 已有实际验证，目标 RTX 5880 实机仍须运行安装验收。

## 1. 隔离与前置

核心模块以 CPython 3.12 Linux 二进制和固定 Docker 镜像交付。PyTorch、vLLM、Jina、数据库、Redis、Qdrant、解析工具等均在容器内，不在宿主机/Conda base 中 pip 安装。

管理员先确认 Docker Engine、Compose v2+、NVIDIA Container Toolkit、系统 Python 3.10+、OpenSSL 3+ 可用。只运行只读检查，不自动升级驱动、重启共享 Docker 或卸载其他项目：

```bash
/usr/bin/python3 --version
openssl version
sudo docker version
sudo docker compose version
nvidia-smi
```

容器固定使用 Torch 2.6.0+cu124、vLLM 0.8.5.post1、Transformers 4.51.3；宿主驱动 580 的 CUDA 13.0 显示不要求将容器改成 CUDA 13。NVIDIA 说明新驱动向后兼容旧 CUDA 应用，但最终仍须实际 CUDA/模型验收：
https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html

Docker 官方 Ubuntu 安装说明包含 Noble 24.04；共享服务器先由管理员评估现有安装，勿直接照抄卸载步骤：
https://docs.docker.com/engine/install/ubuntu/

NVIDIA Container Toolkit 安装说明：
https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

安装器不配置宿主驱动或全局 Docker。GPU 必须空闲，发现计算进程时会拒绝启动，不会杀掉别人的任务。Docker 管理本质上是高权限，普通账号/rootless 尚未承诺支持。

### 原生 CDI 接入（rc.14 起）

RTX5880 示例配置使用 `gpu_access_mode=cdi`。仅对 C-Check 的 Qwen/API 容器指定 `runtime=runc` 和单个 CDI 设备，不改变 Docker 默认 runtime，不重启 Docker，不安装或修复宿主驱动。原生 CDI 显式设置 `NVIDIA_VISIBLE_DEVICES=void`，避免继承镜像变量触发传统重复注入；设备由 CDI spec 注入。API 仍需 NVML，不能仅验证 Qwen。

```bash
sudo docker version --format '{{.Server.Version}}'
sudo docker compose version
sudo nvidia-ctk cdi list
```

Docker Engine 28.3+ 默认启用 CDI，但管理员可能禁用；必须以实际容器测试为准。目标环境 Engine 29.1.3 / Compose 2.40.3 已由接收方验证同一 Qwen 镜像可通过原生 CDI 完成 CUDA 运算，尚不能替代完整模型验收。`gpu` 可填单个索引（如 `0`）或 `nvidia-ctk cdi list` 中对应的 `GPU-...` UUID，推荐 UUID。不要填写 `all` 或完整 `nvidia.com/gpu=...`，安装器负责转换。CDI spec 缺失/失效时联系管理员，不自动升级 Toolkit、修改全局 hook 或重启 Docker。传统机器可显式选择 `gpu_access_mode=legacy`；不会失败后静默回退。

普通账号不能访问 Docker 时，`doctor/install/start/stop/verify/update` 等 Docker 相关命令统一使用 `sudo /usr/bin/python3` 和绝对配置路径；不要依赖 Conda 或 sudo 保留环境。`doctor` 只做只读检查，不占用 GPU；安装在启动模型前会以非 root、只读、无网络、全部丢弃 capabilities 的一次性容器分别验证 CUDA/NVML 及 GPU UUID。启动后再次核验实际服务设备。

`model_download_policy=offline` 只约束模型。完全离线部署必须选择 `runtime_source=image` 并提供完整签名镜像分片；`runtime_source=build` 仍需要下载依赖和访问 apt，不应在断网机器上选择。

### 从 rc.13 未完成安装重试

保留原有外置配置、实例目录、模型、密钥和数据库，不删除或重新初始化。将外置配置增加 `"gpu_access_mode": "cdi"`，并选定 GPU；只替换发布包及 CLI。若实例不存在 `state/deployment.json`，用新版 `install` 重试；它保留已有凭据，且会拒绝接管无 owner 标记的非空目录。若已经存在部署记录，则使用新版 `update --gpu-access-mode cdi --backup-key /独立安全路径/备份密钥`（同时提供常规 config/release/trusted-key 参数），不能用 `install` 静默升级。管理命令默认读取已保存配置，仅修改外置 JSON 不会自动改变已部署实例。不要修改旧签名包、绕过哈希或手工重写托管 Compose。

## 2. 验签并取得完整包

以下使用独立目录，不覆盖已有数据。示例版本为 `@@VERSION@@`，所有 Release 资产缺一不可。

```bash
set -eu
umask 077
mkdir -p "$HOME/c-check-bootstrap-@@VERSION@@"
cd "$HOME/c-check-bootstrap-@@VERSION@@"
BASE=https://github.com/1971936902-byte/C-Check/releases/download/release-@@VERSION@@
for file in publisher.pem release.json release.sig bootstrap.tar.gz; do
  curl -fL --retry 3 "$BASE/$file" -o "$file"
done
printf '%s  publisher.pem\n' @@PUBLISHER_PEM_SHA256@@ | sha256sum -c -
openssl pkeyutl -verify -pubin -inkey publisher.pem -rawin -in release.json -sigfile release.sig
```

任一命令失败立即停止，不执行安装器。上面的公钥 SHA256 必须同时与发布者通过独立可信渠道提供的值核对；不能仅信任同一下载站上的公钥。

验签成功后，核对引导包哈希，再解压可信引导包。不要直接执行未核对的网络脚本：

```bash
/usr/bin/python3 - <<'PY'
import hashlib, json
from pathlib import Path
m = json.loads(Path('release.json').read_text())
p = Path('bootstrap.tar.gz')
assert m['version'] == '@@VERSION@@'
assert p.stat().st_size == m['bootstrap']['size']
assert hashlib.sha256(p.read_bytes()).hexdigest() == m['bootstrap']['sha256']
print('bootstrap verified')
PY
tar -xzf bootstrap.tar.gz
/usr/bin/python3 cli/ccheck.pyz fetch \
  --version @@VERSION@@ \
  --base-url https://github.com/1971936902-byte/C-Check/releases/download \
  --destination "$HOME/c-check-packages" --trusted-key "$PWD/publisher.pem"
```

`fetch` 校验全部签名/哈希，并支持失败后重新执行同一命令续传。包约 5.24GB，不含约 37.4GB 的模型权重。

## 3. 复用已经下载的模型

完整包提供 `site-rtx5880.example.json`，明确使用：

- `/home/lyh/models/Qwen2.5-Coder-32B-Instruct-FP8`
- `/home/lyh/models/jina-code-embeddings-1.5b`

无需重命名模型文件夹，不修改原目录权限，不覆盖或删除原模型。`model_download_policy=offline` 会在导入镜像和创建实例之前逐文件检查签名清单；不匹配时报告具体文件并停止，不自动重下其他版本。名称相同不等于权重版本相同。

签名清单的 Qwen 来源为 `okwinds/Qwen2.5-Coder-32B-Instruct-FP8`，Jina 为包内清单注明的版本。必须同时具有匹配的分片、config、tokenizer 及清单中其余文件。若离线检查失败，请先比较包内 `models/model-manifest.json` 与自己的下载来源，不要删除已有权重，也不要未经确认直接改成 online。

世界可读的相同文件且同一文件系统可硬链接；私有 0600 文件或跨文件系统会复制到专用实例中，不把原文件 chmod 成公共可读。因此极端情况下会多占约 37.4GB。预留至少 80-100GB 空闲并以安装预检为准；1TB 总容量不代表有足够可用空间。

```bash
PKG="$HOME/c-check-packages/release-@@VERSION@@"
cp "$PKG/site-rtx5880.example.json" "$HOME/c-check-site.json"
chmod 600 "$HOME/c-check-site.json"
sudo install -D -m 600 "$PKG/publisher.pem" /etc/c-check/publisher.pem
```

按实际用户名、路径及可用端口修改外置 `c-check-site.json`，不要修改签名包本身。`data_dir` 必须是专用新目录，不能指向 `/home/lyh/models` 或已有其他项目目录。

## 4. 一条安装命令

```bash
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" install \
  --config "$HOME/c-check-site.json" --release "$PKG" \
  --trusted-key /etc/c-check/publisher.pem --version @@VERSION@@
```

命令完成镜像导入、模型校验复用、数据库初始化、服务注册及真实小任务验收。成功显示 READY 才算完成，后台 PID、模型健康或下载完成单独都不算。

默认 0.88 显存占比、32B FP8 权重、BF16 计算/KV auto 路线、8K 上下文、单并发、关闭 n-gram；Jina1.5B 在 CPU 上运行、1536 维。建议不少于 48GB 系统 RAM，资源不足需现场评估，不宣称只要显卡够就一定可用。

初始用户 `admin`，随机密码在专用实例 `secrets/admin_password`，不会设成 admin/admin。只在本机显示密码，勿粘贴进工单或公开日志。

默认 Web 仅监听 `127.0.0.1:18800`；在连接机器的 MobaXterm 中设置 SSH 本地转发至该端口即可。需要局域网开放时用同一新版 CLI 的 `publish --mode lan --bind 0.0.0.0 --port 18800 --public-url http://实际内网IP:18800`，并由管理员控制防火墙。不要额外暴露数据库、Qdrant 或模型端口，不依赖 UFW 自动保护 Docker 发布端口。

## 5. 管理与限制

### rc.15 固定网段与迁移

rc.15 默认 data=`172.30.10.0/26`、inference=`172.30.10.64/26`、edge=`172.30.10.128/26`；这是子网，不是固定每个容器的 IP。前两个网络仍为内部网络，服务名不变。生成、持久化配置和运行时检查使用相同规则。不得仅手改旧 Compose 绕过校验。

先使用新版 CLI 执行 `network-plan --config /绝对路径/site.json`。它只读取 Docker 网络和 IPv4 路由，拒绝与其他项目、LAN/VPN 冲突的网段；不自动换网段或修改宿主路由。若有多个实例或实际冲突，可在外置配置中设置完整的 `network_subnets` 三项，并由管理员确认。已部署实例默认使用已保存配置，不通过编辑外置文件静默改网段。

已有 rc.14 部署使用 `update --backup-key /独立路径/备份密钥`。旧安装尚未完成时使用 `install --backup-key /独立路径/备份密钥`；发现已有网络需要迁移且未提供密钥时，会在停止服务前拒绝继续。网络迁移会暂停并仅重建本项目的容器与网络，不使用 `down -v` 或网络 prune，不删除模型、上传、数据库或凭据。完整实例先做加密一致性备份，部分安装先停止本项目写入者再做加密冷备。

若 rc.14 已有部署记录、但旧网络导致服务无法健康访问，可对新版 `update` 增加 `--cold-network-migration`：它不依赖旧 API 的排空/健康检查，直接停止本项目写入者并加密冷备后再迁移。仅在明确接受中断本项目任务时使用；不得用于绕过网段冲突、归属检查或备份。网络正常时优先普通 update。

备份密钥与发布签名密钥是两种密钥。备份密钥须是独立保存、权限 600 的十六进制文本（例如 `openssl rand -hex 48` 的输出）；妥善离机保管，不使用或复制发布签名私钥。

若断电或中断后检测到 `state/network-migration.json` 为 pending，新版 CLI 会拒绝 start/install/update。使用 `network-recover --config /绝对路径/site.json` 尝试恢复原容器/网络配方，再检查后重试。恢复不回写数据库备份，不删除数据，且保持提交暂停；恢复原 rc.14 时须使用 rc.14 CLI 做版本一致性验证。原配方自动分配子网时，恢复后的地址可能由 Docker 重新分配。恢复也失败时保留维护状态、日志和备份，禁止通过删除迁移标记强行绕过。

本次是部署工具补丁，沿用 rc.14 镜像及模型。完整 `fetch` 可从 rc.14 Release 下载未变化的镜像分片；已有完整 rc.14 包时推荐按 Release 附带的增量说明只下载约 5MB，再核验全包。历史 GPU 门禁不是本次新增 GPU 验收，目标机仍须完成真实安装验收。

```bash
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" status --config "$HOME/c-check-site.json"
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" verify --config "$HOME/c-check-site.json" --fresh
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" logs --config "$HOME/c-check-site.json" qwen --tail 100
```

升级、回退和加密备份必须先阅读项目发布部署操作手册并妥善保管独立备份密钥。不支持任意 schema 或嵌入维度透明回退；切勿 docker system prune 或手动删除共享目录。

Jina 许可涉及 CC-BY-NC-4.0，公司内使用须审查授权，`evaluation`/`authorized` 标记本身不会授予商业许可。二进制提高源码直接读取门槛，不防 root/Docker 管理员复制或逆向；公开仓库已有源代码历史不会消失。

本包不含公司 C 源码、管理员密码、GitHub token、签名私钥或模型权重。5880 本机仍需上述真实验收；成功小样例不等于长期稳定、误报漏报率或并发吞吐证明。
