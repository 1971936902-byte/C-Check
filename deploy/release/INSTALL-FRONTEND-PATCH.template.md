# C-Check @@VERSION@@ 增量部署说明

## 1. 范围和前提

本补丁仅从已正常部署的 **v0.1.0-rc.19 / Qwen2.5-Coder-32B-Instruct-FP8** 升级。
不是 Qwen3/A3B 分支。更早版本先完成 rc.19 升级，不支持从 rc.17 跨模型升级。

- 更新帮助页面：CLI 登录、目录审查、进度、GPU、报告，以及独立服务管理栏目。
- 新增管理员远程终端入口；Guacamole 独立安装、独立账号、默认不启用。
- 仅前端镜像和部署工具变化；后端镜像、审查 CLI、数据库结构、模型、CDI、网络子网及推理参数保持 rc.19。
- 不新增审查后的自动图谱构建。图谱仍按需生成，审查过程中的 RAG 行为不变。
- Guacamole 镜像不包含在补丁内，启用时按 terminal/images.lock.json 的固定摘要下载。

保留完整 rc.19 包、站点配置、实例数据、模型、用户密钥及独立可信发布公钥。
公钥文件 SHA256：`@@PUBLISHER_PEM_SHA256@@`。不要用补丁里附带的公钥建立新的信任。
无需发布私钥，不要覆盖现有密码或执行 down -v。升级安排本项目维护窗口，等待审查任务完成。
部署器仍可能重建/重启本项目服务并进行模型预检和验收，并非零停机或纯浏览器热更新。
无新增数据库迁移。为升级失败准备可用的原备份密钥与加密备份；不要凭示例路径新建或覆盖旧密钥。
磁盘至少容纳新补丁、前端镜像、未变镜像的复用空间以及实例加密备份和 3GiB 安全余量。

## 2. 解压和签名校验

将补丁及 .sha256 文件放到 /home/lyh/c-check-packages；使用 bash 执行下列步骤。
外层 SHA256 仅检查传输，真实性由原可信公钥验证的 release.sig 保证。

```bash
set -euo pipefail
cd /home/lyh/c-check-packages
sha256sum -c release-@@VERSION@@-patch.tar.gz.sha256
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.19
NEW=/home/lyh/c-check-packages/release-@@VERSION@@
TRUST=/home/lyh/c-check-instance/config/publisher.pem
SITE=/home/lyh/c-check-site.json
test ! -e "$NEW"
tar -xzf release-@@VERSION@@-patch.tar.gz --no-same-owner
sudo openssl pkeyutl -verify -pubin -inkey "$TRUST" -rawin \
  -in "$NEW/release.json" -sigfile "$NEW/release.sig"
sudo openssl pkeyutl -verify -pubin -inkey "$TRUST" -rawin \
  -in "$OLD/release.json" -sigfile "$OLD/release.sig"
```

## 3. 从 rc.19 本地复用镜像

以下代码在执行包内程序之前验证全部文件，不下载模型，不修改旧包。
同文件系统优先使用硬链接；不要修改任何复用文件，跨文件系统则复制并额外占空间。
旧目录必须为完整 rc.19 发布包，不是只含元数据的目录。

```bash
sudo python3 - "$OLD" "$NEW" <<'PY'
import hashlib, json, os, shutil, sys
from pathlib import Path
old, new = map(Path, sys.argv[1:])
def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''): h.update(block)
    return h.hexdigest()
def safe(root, name):
    p = root/name
    assert not Path(name).is_absolute() and '..' not in Path(name).parts
    assert not any(x.is_symlink() for x in [p,*p.parents])
    assert p.resolve().is_relative_to(root.resolve())
    return p
a = json.loads((old/'release.json').read_text())
b = json.loads((new/'release.json').read_text())
assert a['version'] == 'v0.1.0-rc.19' and b['version'] == '@@VERSION@@'
assert sha(old/'release.json') == b['distribution']['upgrade_from_sha256']
assert a['schema_epoch'] == b['schema_epoch']
for role in ('backend','qwen','embedding','mysql','redis','qdrant'):
    assert a['images'][role] == b['images'][role]
assert a['files']['models/model-manifest.json'] == b['files']['models/model-manifest.json']
for name in b['distribution']['reused_files']:
    assert name.startswith('images/') and a['files'][name] == b['files'][name]
    src, dst = safe(old,name), safe(new,name)
    assert sha(src) == b['files'][name], name
    dst.parent.mkdir(parents=True,exist_ok=True)
    if not dst.exists():
        try: os.link(src,dst)
        except OSError: shutil.copy2(src,dst)
for name, expected in b['files'].items():
    p = safe(new,name)
    assert p.stat().st_size == b['artifact_sizes'][name] and sha(p) == expected, name
print('RC20_ALL_SIGNED_FILES_VERIFIED; Qwen2.5/Jina/backend unchanged')
PY
sudo python3 "$NEW/cli/ccheck.pyz" package-verify --release "$NEW" --trusted-key "$TRUST"
```

## 4. 更新并验收

用实际已存在的备份加密密钥路径替换下面示例；密钥缺失先解决，不要临时生成替代旧密钥。

```bash
BACKUP_KEY=/实际安全目录/原备份密钥.key
sudo test -f "$BACKUP_KEY"
sudo python3 "$NEW/cli/ccheck.pyz" update --config "$SITE" --release "$NEW" \
  --version @@VERSION@@ --trusted-key "$TRUST" --backup-key "$BACKUP_KEY"
sudo python3 "$NEW/cli/ccheck.pyz" status --config "$SITE"
sudo python3 "$NEW/cli/ccheck.pyz" verify --config "$SITE"
```

刷新或强制刷新浏览器，确认旧账号、任务和报告仍在；帮助页面有 CLI 使用、服务管理、Web 使用三个栏目。
管理员可看到“远程终端”入口，未配置时禁用，这是默认行为；普通账号不显示入口。
检查默认模型仍为 Qwen2.5-Coder-32B-FP8，提交小型实际审查并确认报告和 CLI 正常。
本次构建不宣称完成 RTX5880 真机推理、吞吐或准确率验收。
失败时保留日志与备份，不删除实例或重启宿主 Docker；根据部署器状态选择同 schema 回滚或原加密备份恢复。
如已新增 web.terminal_config，旧 rc.19 CLI 不识别该字段，应继续使用本次新 CLI 管理或回滚；不要改写实例内部配置以绕过校验。

## 5. 可选：启用远程终端

Guacamole 不随 C-Check update 自动启动。先阅读包内 terminal/README.md，再将 terminal 目录复制到独立 /opt/c-check-terminal，按文档初始化。
有现成 Guacamole 服务也可只配置链接，不必重复安装。生产使用可信 HTTPS、访问源限制、独立普通 SSH 用户。
默认终端只监听回环地址，需要 SSH 隧道；浏览器地址必须是浏览器所在电脑能访问的地址。
测试 self-signed 证书和预览前端不等于生产 TLS 部署，不要开放模拟 API 预览。

在签名包之外创建公开链接配置（不放账号、密码、私钥或令牌）：

```bash
sudo install -d -m 755 /etc/c-check
printf '%s\n' '{"url":"https://terminal.example.com/guacamole/"}' | sudo tee /etc/c-check/terminal-config.json >/dev/null
sudo chmod 644 /etc/c-check/terminal-config.json
```

将实际 /home/lyh/c-check-site.json 的 web 对象增加以下字段，保留其余配置：

```json
"terminal_config": "/etc/c-check/terminal-config.json"
```

建议在第 4 步更新之前完成。已更新后才配置时，再用同一 rc.20 CLI 执行 update，使站点配置和受管理 Compose 一致。
配置会只读挂载到前端，后续启停/校验认可它；不要手改 state/compose.json，也不要修改签名包内 terminal-config.json。
以后只改 URL 可原位修改外部文件并刷新页面；若用编辑器原子替换文件，需要通过 update 重建容器重新绑定新文件。
路径必须存在、不是符号链接，JSON 仅允许 url 字段，地址只接受不带凭据/查询/片段的绝对 HTTPS URL。
入口只是新窗口链接，Guacamole 独立登录，不共享 C-Check JWT；其服务在 C-Check 停止时仍可独立使用。

## 6. 编译记录

verify/build-gates.json 记录本次源码、前端、部署工具及隔离验证；verify/rc19-build-gates.json 保留历史基线证据。
后端、模型、原生核心轮子和数据库未改动，不混入 Test 分支后端实验；部署终端的历史 CPU 服务器验证范围见 terminal/VERIFICATION.md。
构建脚本 deploy/release/build_frontend_patch.py 和相关源码保留在独立 codex/rc20-frontend-patch 分支。
