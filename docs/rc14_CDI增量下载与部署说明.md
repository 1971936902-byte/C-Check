# rc.14 原生 CDI 增量下载与部署

适用条件：保留了完整 rc.13 发布包，尤其是其中的 `images/` 目录。仅在 Docker 中导入过镜像、但已删除原始镜像分片，不满足本方法的条件。

## 结论

rc.14 的 7 个镜像身份、9 个镜像分片的 SHA256，以及模型清单均未变化。推荐新建 rc.14 目录，复用旧镜像分片，下载新版引导包和签名元数据，再按新版清单验证整个包。

- 网络下载约 5.24 MB，而不是完整发布附件约 5.24 GB。
- 模型不重新下载；已导入且身份匹配的镜像不重复导入。
- 不覆盖旧包，不修改签名文件，不重新压缩旧镜像。
- 先保留 rc.13 作为回溯材料；旧版本的 GPU 接入缺陷不会因此修复，正式部署须使用 rc.14 CLI。
- 镜像复用不代表免验收：目标 RTX5880 仍需完成模型加载、API 遥测和小任务验收。

## 1. 准备两个独立目录

以下路径按实际旧包位置修改。不要将发布包目录指向模型目录或实例数据目录。若新目录已存在，先核实内容，不要直接删除或覆盖。

```bash
set -eu
umask 077
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.13
NEW=/home/lyh/c-check-packages/release-v0.1.0-rc.14
TRUST=/etc/c-check/publisher.pem
BASE=https://github.com/1971936902-byte/C-Check/releases/download/release-v0.1.0-rc.14
test -d "$OLD/images"
test -f "$OLD/publisher.pem"
test ! -e "$NEW"
mkdir -p "$NEW"
cd "$NEW"
```

## 2. 下载并验证新版元数据

公钥保持不变。下列指纹须与发布者通过独立可信渠道提供的指纹一致；不能仅信任同站下载的公钥。

```bash
printf '%s  %s\n' \
  9d1e2a1fe9de3b4245f3abaaa72ccc8ef19e8322e0bc7a3de878353561d86e37 \
  "$OLD/publisher.pem" | sha256sum -c -
for file in release.json release.sig bootstrap.tar.gz checksums.sha256; do
  curl -fL --retry 3 "$BASE/$file" -o "$file"
done
openssl pkeyutl -verify -pubin -inkey "$OLD/publisher.pem" \
  -rawin -in release.json -sigfile release.sig
```

签名失败立即停止。签名通过后，核对版本、引导包及校验清单的哈希：

```bash
/usr/bin/python3 - <<'PY'
import hashlib, json
from pathlib import Path
m = json.loads(Path('release.json').read_text())
assert m['version'] == 'v0.1.0-rc.14'
assert m['tag'] == 'release-v0.1.0-rc.14'
for name in ['bootstrap.tar.gz', 'checksums.sha256']:
    p = Path(name)
    assert p.stat().st_size == m['artifact_sizes'][name], name
    assert hashlib.sha256(p.read_bytes()).hexdigest() == m['files'][name], name
print('signed metadata and bootstrap verified')
PY
tar -xzf bootstrap.tar.gz
```

仅在上述验证成功后解压或运行新的 CLI。引导包含新版 CLI、安装文档、示例配置、依赖清单、许可及二进制组件，不包含模型权重或镜像分片。

## 3. 复用镜像分片并进行全包验证

推荐使用隔离副本，支持 reflink 的文件系统可自动节省空间，否则本地复制约 5.23 GB。它不产生相同体积的网络下载，也不修改旧包。

```bash
cp -a --reflink=auto "$OLD/images" "$NEW/images"
if ! sudo test -e "$TRUST"; then
  sudo install -D -m 600 "$OLD/publisher.pem" "$TRUST"
fi
printf '%s  %s\n' \
  9d1e2a1fe9de3b4245f3abaaa72ccc8ef19e8322e0bc7a3de878353561d86e37 \
  "$TRUST" | sudo sha256sum -c -
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" package-verify \
  --release "$NEW" --trusted-key "$TRUST"
```

`package-verify` 按 rc.14 的新签名清单检查所有文件，不只检查镜像 ID。缺分片、内容被修改、错误版本文件或多余文件均应先处理，禁止跳过验证。

通过 sudo 运行时，可信公钥必须由 root 管理且不能被其他用户写入，因此不能直接使用普通用户拥有的下载公钥文件。若已有 `/etc/c-check/publisher.pem` 指纹不同，立即停止，不覆盖现有信任锚。

若只剩 Docker 中的镜像、没有旧 `images/` 分片，请使用完整包下载流程。不要通过 `docker save` 重新导出并重压缩来冒充原分片，其字节及哈希通常不同。

此目录为手工拼装的完整签名包，直接用于 `package-verify/install/update`。不要向该非空目录再次执行 `fetch`；完整下载需另外选择新的下载目录。

## 4. 配置并部署

保留现有实例目录、模型、数据库和凭据。修改外置站点 JSON，而不是发布包中的示例文件；关键项为：

```json
{
  "gpu": "0",
  "gpu_access_mode": "cdi",
  "runtime_source": "image",
  "model_download_policy": "offline"
}
```

这是关键字段片段，不是完整配置。`gpu` 也可替换为 `sudo nvidia-ctk cdi list` 中的单张 GPU UUID，推荐 UUID；不要填 `all` 或 `nvidia.com/gpu=...`。模型目录仍使用已有 `/home/lyh/models/` 下的两个目录。

若 rc.13 安装未完成，且实例中没有 `state/deployment.json`，使用新版安装器重试：

```bash
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" install \
  --config /home/lyh/c-check-site.json --release "$NEW" \
  --trusted-key "$TRUST" --version v0.1.0-rc.14 \
  --gpu-access-mode cdi
```

若已存在成功部署记录，不能用 install 静默升级。使用新版 update，并提供独立保存的加密备份密钥：

```bash
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" update \
  --config /home/lyh/c-check-site.json --release "$NEW" \
  --trusted-key "$TRUST" --version v0.1.0-rc.14 \
  --gpu-access-mode cdi --backup-key /独立安全路径/备份密钥
```

最后一个路径必须替换为符合备份工具要求的真实密钥，不能使用签名私钥。管理命令默认读取已保存的站点配置；完整安装文档为新包中的 `INSTALL-RTX5880.md`。

## 5. 边界

- 不重启共享 Docker，不更换其全局默认 runtime，不强制修复混装的 NVIDIA 570/580 软件包。
- 非 root、只读、单卡设备范围及部署一致性检查保持有效；API 的 NVML 和 Qwen 的 CUDA 都必须通过。
- 发现已有计算任务时不会杀进程强占 GPU，需等待空闲窗口。
- 只有显示 READY 且实际验收通过，才算平台部署完成。
- 打包服务器上的门禁及一次性 CDI 运算，不等同于目标 RTX5880 的完整模型验收。
