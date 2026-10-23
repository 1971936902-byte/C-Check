# C-Check 方案二：轻量在线部署 @@VERSION@@

## 发布边界

本方案与离线 rc16 并存，不删除、不覆盖旧包。发布目录不包含镜像分片、模型权重、核心 wheel
和编译器；核心已编译到 backend 镜像中，安装时下载。Python 库、PyTorch、vLLM、CUDA 用户态库
均在锁定的容器镜像内，不向宿主机 pip/Conda 环境安装。Docker Compose 管理服务，
components.lock.json 统一记录镜像身份、OCI digest、Python 版本与 wheel 哈希、模型 revision/SHA256。
部署现场不重新解析依赖，不使用 latest 标签，不自动升级驱动或 Docker。

镜像缺失时：MySQL/Redis/Qdrant 优先按 digest 从容器仓库拉取；不可达时使用签名清单中的
HTTPS 分片回退。应用、前端和 AI 运行镜像按固定 SHA256 从现有发行资源下载，校验后导入。
所以本方案首版仍依赖保留的 rc14 镜像资源，并非已迁移至独立 GHCR/Harbor 仓库。
模型优先校验本地文件，缺失才按固定清单下载。首次总下载量及最终磁盘占用不会凭空消失。

## 前置准备

Linux amd64；Python 3.10+、OpenSSL 3、Docker Engine/Compose、NVIDIA 驱动/Toolkit/CDI
由管理员准备。共享服务器禁止为了安装本项目而重启 Docker、替换驱动或改全局网络池。
缓存目录需部署用户拥有，权限 0700；sudo 安装时应让 root 创建新的专用缓存目录。
Docker 登录凭据放 Docker 自身凭据存储，不写进站点文件、发行包或聊天。

磁盘：除模型/镜像最终占用外预留下载缓存、解压、数据库、备份空间；首次新装建议可用空间
不少于 100GB，既有完整缓存可降低需求，安装器仍按实际缺失内容检查预算。
48GB 目标可启用 performance 的 Jina GPU 配置；40GB A100 必须删除 performance，使用原
CPU 嵌入配置。RTX5880 双模型共同驻留仍须目标机安装验收，不能由 A100 结果推断已通过。

## 信任与配置

独立确认原发布公钥 PEM SHA256：
`@@PUBLISHER_PEM_SHA256@@`。
先核验 release.json 的 Ed25519 签名和 bootstrap 哈希，再解压；package-verify 必须成功。
不得把刚下载的公钥直接当作新的信任锚。

将 site-online.example.json 复制到包目录之外，修改实例目录、端口、站点地址、模型路径、
三个不冲突的子网、GPU 编号/UUID 和 delivery.cache_dir。
默认在线允许下载；仅复用模型可设置 model_download_policy=offline；完全禁止下载还应
设置 delivery.offline=true。镜像缺失且禁止下载时明确失败，不回退到未知版本。
可选 delivery.artifact_base_url 为企业 HTTPS 分片镜像目录，保持原文件名和哈希；
registry_mirrors 如 {"docker.io":"harbor.company.example/dockerhub"} 只改来源，保留 digest。

```bash
PKG=/home/lyh/c-check-packages/release-@@VERSION@@
SITE=/home/lyh/c-check-online-site.json
TRUST=/etc/c-check/publisher.pem
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" package-verify --release "$PKG" --trusted-key "$TRUST"
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" download-plan --config "$SITE" --release "$PKG" --trusted-key "$TRUST"
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" prepare-assets --config "$SITE" --release "$PKG" --trusted-key "$TRUST"
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" network-plan --config "$SITE"
sudo /usr/bin/python3 "$PKG/cli/ccheck.pyz" install --config "$SITE" --release "$PKG" --trusted-key "$TRUST"
```

配置好后，最后一条 install 即可自动完成缺失资源下载、容器创建、模型启动及真实小任务验收。
prepare-assets 是可选预下载，不启动任何业务服务、不占 GPU；网络不稳定时建议先完成它。
下载支持重试、断点续传、哈希校验及跨部署缓存；校验失败不会把不完整文件发布为有效资源。
失败后使用同一命令重试，不删除实例数据，不重复创建随机凭据。
初始用户名 admin，随机密码位于实例 secrets/admin_password，非 admin/admin。

## 更新与验证

已有实例迁移前保留原发行包和外置备份密钥，先运行 prepare-assets，下载失败不进入停机阶段。
随后使用 update --backup-key /绝对路径/原备份密钥；如需从 CPU 切 GPU，显式加入
--performance-profile shared-gpu。其余站点配置沿用保存的实例配置；delivery 来源设置可从外置文件读取。
管理密码改过时初始验收凭据可能不匹配；不要清库，保留错误并联系维护者。

READY 后运行 verify；检查实际报告、已知漏洞、资源监控和 GPU。相同组件版本不代表不同 GPU
的吞吐相同。旧离线方案继续可用；回退使用原版本包和一致性备份流程，不使用 down -v 或 prune。
本方案仅轻量化交付，不提升核心源码的保密强度；主机 root 仍可检查容器内容。
