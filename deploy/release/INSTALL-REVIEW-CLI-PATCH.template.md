# C-Check @@VERSION@@：CLI 与实时进度增量补丁

## 适用范围与变更

从完整、正常部署的 **v0.1.0-rc.18 / Qwen2.5-Coder-32B-Instruct-FP8** 升级。
rc.16 及更早版本先按原说明升级到 rc.18；不适用于 rc.17/Qwen3。

- 新增 `ccheck-review` 0.2.1：提交目录、状态/进度、报告下载、GPU 监控与 `help`，共 10 个子命令。
- 支持 C/H 和 `_c.txt`/`_h.txt`，复用既有审查流程，不修改模型或提示词。
- 新增准确的第一阶段扫描文件计数。所有计划分片成功才计为文件完成；函数单元计划文件数可能少于上传数。
- GPU 轻量接口沿用管理员权限；显存和计算利用率分开显示，不把共享 GPU 用量称为任务独占用量。
- 仅 backend 镜像变化，重新编译 `model_router`；其余 11 个核心二进制复用。前端及 Qwen/Jina/MySQL/Redis/Qdrant 镜像、模型权重和依赖锁不变。
- 维持 CDI、非 root、只读容器及原有站点配置，不更换模型或调整推理参数。

签名公钥文件 SHA256：`@@PUBLISHER_PEM_SHA256@@`。必须使用此前独立保存的可信公钥，不要仅信任同一下载来源的公钥。

## 升级前

保留完整 rc.18 发布目录、站点配置、数据、账号、密码、上传文件、模型和密钥。不要重新初始化、执行 `down -v` 或删除数据。
安排本项目维护窗口并等待在途任务完成。升级不会重启 Docker 或操作其他项目。
准备原有备份加密密钥（实例目录之外，0600 权限，单行十六进制文本）；不要覆盖密钥，也不需要发布签名私钥。
预留新包、镜像和加密备份空间；备份需实例数据大小、文件开销和至少 3GiB 安全余量。

## 验签与增量下载

按实际路径调整变量。`OLD` 必须包含已验证的完整 rc.18 镜像分片；不完整缓存需要额外从历史 Release 下载缺失分片。
使用新引导 CLI：rc.18 CLI 不认识本次新迁移和跨版本复用分片映射。

```bash
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.18
DEST=/home/lyh/c-check-packages
NEW="$DEST/release-@@VERSION@@"
TRUST=/home/lyh/c-check-trust/publisher.pem
SITE=/home/lyh/c-check-site.json
BACKUP_KEY=/home/lyh/c-check-secrets/backup.key
BOOT="$DEST/rc19-bootstrap"
URL=https://github.com/1971936902-byte/C-Check/releases/download/release-@@VERSION@@
umask 077
mkdir -p "$BOOT"
for name in release.json release.sig bootstrap.tar.gz; do
  curl --fail --location --proto '=https' --proto-redir '=https' "$URL/$name" -o "$BOOT/$name" || exit 1
done
sudo openssl pkeyutl -verify -pubin -inkey "$TRUST" -rawin \
  -in "$BOOT/release.json" -sigfile "$BOOT/release.sig" || exit 1
python3 - "$BOOT" <<'PY'
import hashlib, json, sys, tarfile
from pathlib import Path
root = Path(sys.argv[1])
release = json.loads((root/'release.json').read_text())
assert release['version'] == '@@VERSION@@'
archive = root/'bootstrap.tar.gz'
assert archive.stat().st_size == release['bootstrap']['size']
assert hashlib.sha256(archive.read_bytes()).hexdigest() == release['bootstrap']['sha256']
with tarfile.open(archive, 'r:gz') as source:
    member = source.getmember('cli/ccheck.pyz')
    assert member.isfile() and member.size == release['artifact_sizes']['cli/ccheck.pyz']
    data = source.extractfile(member).read()
assert hashlib.sha256(data).hexdigest() == release['files']['cli/ccheck.pyz']
(root/'ccheck.pyz').write_bytes(data)
print('VERIFIED_BOOTSTRAP_CLI')
PY
test "$?" -eq 0 || exit 1
sudo python3 "$BOOT/ccheck.pyz" fetch --version @@VERSION@@ \
  --base-url https://github.com/1971936902-byte/C-Check/releases/download \
  --destination "$DEST" --trusted-key "$TRUST" --reuse-release "$OLD"
sudo "$NEW/ccheck" package-verify --release "$NEW" --trusted-key "$TRUST"
```

完整 rc.18 缓存会复用所有未变镜像，只下载新后端分片和引导材料。不会重新下载模型。
没有完整缓存时，前端分片来自 rc.18，旧运行时分片来自签名清单指定的历史版本，均校验哈希。
不要修改包内签名文件或用重新 docker save 的输出冒充原分片。

## 更新与验收

```bash
sudo "$NEW/ccheck" update --config "$SITE" --release "$NEW" --version @@VERSION@@ \
  --trusted-key "$TRUST" --backup-key "$BACKUP_KEY"
sudo "$NEW/ccheck" version --config "$SITE"
sudo "$NEW/ccheck" status --config "$SITE"
sudo "$NEW/ccheck" verify --config "$SITE"
```

部署器先备份，再同步更新 API/Worker 并迁移至 `0017_review_scan_progress`。新增两个可空统计列，不删除旧数据，不回填历史估算值。
迁移/验收失败时会保持维护状态并停止写入，不自动把旧镜像连到新数据库。保留 `ENCRYPTED_BACKUP` 路径及密钥，按匹配 rc.18 的完整备份恢复；不要直接 image rollback、手改 schema_epoch 或删除 alembic_version。

## 使用审查 CLI

部署管理命令仍是 `ccheck`；审查命令是 `ccheck-review`，二者不要混用。审查 CLI 只需 Python 3.10+，无需 sudo、GPU 或额外 Python 库。
若下载目录为 root 私有，先由管理员将**已验签**的单文件 CLI 安装为系统命令（不含令牌、密码）：

```bash
sudo install -m 755 "$NEW/cli/ccheck-review.pyz" /usr/local/bin/ccheck-review
ccheck-review help
ccheck-review login --server http://127.0.0.1:8800 --username YOUR_USERNAME
ccheck-review submit --name WirelessTest --dir /home/lyh/c-check-test-data/E5_Wireless_txt --wait --gpu
ccheck-review status TASK_ID --wait --gpu --poll-interval 2
ccheck-review report TASK_ID --output ./report.pdf
ccheck-review gpu --watch --poll-interval 2
```

将 8800 换成实际站点端口，使用已有账号和密码，登录时交互输入密码。配置/令牌保存在当前用户的 `~/.config/ccheck-review/`。
非回环 HTTP 默认拒绝，建议 HTTPS/SSH 转发；受控内网确需明文 HTTP 时登录显式加 `--allow-insecure-http`。
普通账号可审查自己的任务；GPU 监控需要管理员权限，无权限只提示不可用，不影响审查。

验收时确认历史数据和模型配置保留、CLI 新任务在 Web 可见、进度可更新、报告可下载。
`remaining=0` 只是第一阶段计划文件扫描结束，仍可能进行后处理；以任务 `completed` 为完成依据。
历史任务、尚未制定计划时计数为 N/A。CLI 其他参数和 JSON 集成见包内 `CLI-REVIEW.md`。

构建证据见 `verify/build-gates.json`。本次未重新做 RTX5880 真机吞吐/准确率测试；不宣称模型能力提升。
