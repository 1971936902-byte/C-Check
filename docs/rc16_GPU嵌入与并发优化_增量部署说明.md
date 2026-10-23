# rc16 GPU 嵌入与并发优化：增量部署说明

## 一、变更与限制

适用目标：Linux amd64、RTX5880 Ada 48GB、现有完整 rc14/rc15 包和两个本地模型。

| 项目 | rc15 原配置 | rc16 shared-gpu |
|---|---|---|
| Jina 1.5B | CPU FP32 | GPU BF16 |
| Jina 微批量 | 1 | 1，支持受控设置为 2 |
| Qwen 显存比例 | Ada 0.88 | 0.82，为 Jina 留余量 |
| vLLM 并行序列 | 1 | 2 |
| 调度 Token 预算 | 4096 | 8192 |
| 审查片段请求并发 | 1 | 2 |
| 完整任务 Worker 并发 | 1 | 1，不变 |
| 上下文 / 输出上限 | 8192 / 2048 | 不变 |
| KV / n-gram | auto/BF16 / 关闭 | 不变 |

不是扩大所有参数。两模型共享同一卡，需要给 Jina 的权重和长输入激活留显存。
最大调度 Token 数不等于上下文长度，也不保证 2 条最长请求同时不排队。
未改变模型权重、审查提示词、问题筛选和合并逻辑。BF16 数值会与 CPU FP32 略有差异，
不承诺报告逐字一致或准确率必然提升。

复用原 7 个镜像及其分片。GPU Jina 容器使用原 Qwen CUDA 镜像，而非 CPU 嵌入镜像；
仅更新签名 CLI 内的启动资源、配置和校验逻辑。无需下载模型或在宿主机安装 PyTorch。
核心审查二进制不变；公开的运行胶水脚本不包含核心审查实现。

重要验证边界：构建机只有 A100 40GB。本次分别验证 Jina GPU 与 Qwen 双请求，
不代表已在 RTX5880 上完成两模型共同驻留、长输入并发或全量任务验收。
最终必须在目标机完成安装小任务和 verify，再跑实际项目对比耗时、显存、报告质量。

## 二、下载、验签与组装

以下路径按现场修改。`OLD` 指完整包，不能指只有 7 个小文件的 patch 目录。
保持原信任公钥，不把刚下载的公钥直接当作信任锚。

```bash
set -eu
umask 077
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.15
NEW=/home/lyh/c-check-packages/release-v0.1.0-rc.16
SITE=/home/lyh/c-check-site.json
TRUST=/etc/c-check/publisher.pem
BACKUP_KEY=/etc/c-check/backup-keys/rtx5880.hex
BASE=https://github.com/1971936902-byte/C-Check/releases/download/release-v0.1.0-rc.16
test -d "$OLD/images"
test -f "$SITE"
test ! -e "$NEW"
mkdir -p "$NEW"
cd "$NEW"
for file in release.json release.sig bootstrap.tar.gz checksums.sha256; do
  curl -fL --retry 3 "$BASE/$file" -o "$file"
done
printf '%s  %s\n' \
  9d1e2a1fe9de3b4245f3abaaa72ccc8ef19e8322e0bc7a3de878353561d86e37 \
  "$TRUST" | sudo sha256sum -c -
sudo openssl pkeyutl -verify -pubin -inkey "$TRUST" \
  -rawin -in release.json -sigfile release.sig
python3 - <<'PY'
import hashlib, json
from pathlib import Path
m=json.loads(Path('release.json').read_text())
assert m['version']=='v0.1.0-rc.16'
assert m['distribution']['base_version']=='v0.1.0-rc.14'
for name in ['bootstrap.tar.gz','checksums.sha256']:
    p=Path(name)
    assert p.stat().st_size==m['artifact_sizes'][name]
    assert hashlib.sha256(p.read_bytes()).hexdigest()==m['files'][name]
print('Signed bootstrap hashes verified')
PY
tar -xzf bootstrap.tar.gz
cp -a --reflink=auto "$OLD/images" "$NEW/images"
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" package-verify \
  --release "$NEW" --trusted-key "$TRUST"
```

rc16 镜像下载来源仍指向完整 rc14 Release，而不是没有镜像附件的 rc15。
本地可复用完整 rc14 或 rc15 的 images/，由新版完整清单校验每个分片。
若只有 Docker 已导入镜像，没有原分片，请用 rc16 CLI 的 fetch 在新目录下载完整包；
不能重新 docker save 后冒充原签名分片。不要在签名包目录内放配置或额外笔记。

## 三、执行更新

先保留外置配置备份，不覆盖实例中已有密钥。备份密钥沿用 rc15 的文件，确认有离机备份。
确认所选 GPU 没有其他人的工作负载。本次不修改 Docker daemon、不重启 Docker、不改驱动。

```bash
cp -p "$SITE" "$SITE.before-rc16"
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" network-plan --config "$SITE"
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" update \
  --config "$SITE" --release "$NEW" --version v0.1.0-rc.16 \
  --trusted-key "$TRUST" --backup-key "$BACKUP_KEY" \
  --performance-profile shared-gpu
```

必须显式传入 `--performance-profile shared-gpu`。已有实例优先使用其保存的站点配置，
只编辑外置示例文件或仅更新包版本，不会自动启用 GPU 嵌入。
本次沿用原网段、端口、GPU 编号/UUID 和 CDI 模式；正常 rc15 升级不需要网络迁移。
安装器排空任务、加密备份，停止本项目两个模型，先启动 Qwen，待其完成显存分配后启动 Jina，
再启动应用并执行真实小任务。仅影响本项目，不删除模型、数据库、上传文件或历史报告。

新部署使用新版 example 配置（已有 performance 配置）和 install；仍需配置绝对模型路径。
不要把 shared-gpu 配置用于 40GB A100 原评估实例；该 48GB 配置未在 40GB 上共同驻留验收。

## 四、验证

```bash
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" status --config "$SITE"
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" verify --config "$SITE"
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" logs --config "$SITE" embedding --tail 80
nvidia-smi
```

期望：READY；embedding health 为 cuda:0、torch.bfloat16、1536 维；Qwen 配置为
max_num_seqs=2、max_num_batched_tokens=8192；API/Worker 的片段并发为 2。
非 root、只读根文件系统、最小设备范围、capabilities 和签名校验保持不变。
脚本按内容哈希命名、只读挂载，verify 会核验脚本内容及实际 GPU 设备归属。

初始管理员密码已改过时，旧验收程序仍可能读取初始文件导致登录失败。
不要为通过验收而清库、删除 deployment.json 或修改初始文件。请保留错误，联系维护者。

Jina GPU 已有受控小批量和输入上限，但单条长输入不会被悄悄截断。
出现显存不足会返回明确错误，不静默切 CPU 或切换哈希向量。
看到 503/OOM、模型重启、明显 KV 抢占或报告异常，应停止压测，使用下面的 CPU 回退。

## 五、回退与运维

先保持 rc16 包，使用同一已签名版本恢复 CPU 嵌入及原串行参数：

```bash
sudo /usr/bin/python3 "$NEW/cli/ccheck.pyz" update \
  --config "$SITE" --release "$NEW" --version v0.1.0-rc.16 \
  --trusted-key "$TRUST" --backup-key "$BACKUP_KEY" \
  --performance-profile cpu-serial
```

保留 rc14/15 完整包、实例数据、签名信任锚和备份密钥。更新失败会尝试恢复此前配置，
可能保持维护状态；先检查 status 和日志，不手动清理数据库或全局 Docker 资源。
如需完整版本回退，先恢复 CPU 配置并核验，再按原备份/版本流程操作，不混用新旧 CLI。

GPU 加速重点改善首次索引和查询嵌入；双请求改善片段吞吐，不能承诺总任务固定提速比例。
验证至少比较同一项目的首次索引耗时、缓存命中、审查总耗时、OOM/抢占、报告数和已知漏洞召回。
