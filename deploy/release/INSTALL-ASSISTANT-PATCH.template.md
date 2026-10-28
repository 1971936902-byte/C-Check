# C-Check @@VERSION@@ 合并增量补丁

## 适用范围

本补丁从完整、正常部署的 **v0.1.0-rc.20 / Qwen2.5-Coder-32B FP8** 升级。
仍为 rc19 时先按原 rc20 说明升级到 rc20；更早版本先完成原有升级路径。
不支持直接从 A3B/Qwen3 分支升级，也不更换 Qwen 或 Jina 权重、推理镜像、模型路径及原有性能配置。

新增内容：AI 智能助手、个人知识文件 RAG、审查优先的对话抢占、受管远程 SSH 终端及独立帮助章节。
审查流程与按需生成图谱的行为不变。助手默认不启用；终端是独立 CPU 服务，默认不安装启动。
后端、前端和新增 AnythingLLM 提供方镜像需要下载；不重复下载模型权重。
本次数据库包含四项新增助手迁移，必须先创建加密备份，不是仅替换前端文件。

发布者公钥文件 SHA256：`@@PUBLISHER_PEM_SHA256@@`。
请对照以前独立保存的可信公钥。不要仅因包内提供 publisher.pem 就信任它，不得修改签名文件。

## 升级前

- 保留完整旧发布包、实例目录、配置、账号、密码、模型、数据库和原备份密钥。
- 留出维护窗口，结束在途审查和图谱任务；升级会重启本项目服务，不会重启全局 Docker。
- 准备实例目录之外、仅所有者可读的备份加密密钥。不存在旧密钥且从未创建备份时，可在安全新路径生成单行十六进制密钥；不能覆盖旧密钥。
- 留出新镜像/发布包、完整实例备份及至少 3 GiB 安全余量。不要通过删除模型、数据库、密钥或旧发布物腾空间。
- 沿用现有 GPU UUID、CDI/legacy、网络网段和模型配置，不传 model-migration 或 performance-profile。
- 需要对话与 RAG 时，在外置站点 JSON 顶层加入 `"assistant": {"enabled": true}`；其他配置不改。详细限制和恢复步骤见 ASSISTANT.md。

## 获取完整、可校验的新发布目录

以下变量按实际环境修改。OLD 必须是包含原始镜像分片的完整已验证 rc20 包，不是只有少量附件的 patch 文件夹。
若发布目录由 root 管理，使用下面的 sudo 和绝对路径，不必切换进入 root-only 目录。

```bash
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.20
DEST=/home/lyh/c-check-packages
NEW="$DEST/release-@@VERSION@@"
SITE=/home/lyh/c-check-site.json
TRUST=/home/lyh/c-check-instance/config/publisher.pem
BACKUP_KEY=/home/lyh/c-check-secrets/backup.key

sudo "$OLD/ccheck" package-verify --release "$OLD" --trusted-key "$TRUST"
sudo "$OLD/ccheck" fetch \
  --version @@VERSION@@ \
  --base-url https://github.com/1971936902-byte/C-Check/releases/download \
  --destination "$DEST" --trusted-key "$TRUST" --reuse-release "$OLD"
sudo "$NEW/ccheck" package-verify --release "$NEW" --trusted-key "$TRUST"
```

每一步成功后再执行下一步。已有完整 rc20 包时复用经过签名和哈希核对的原始分片；
缺少旧分片时仍需从原 Release 下载，下载量会增加。fetch 可以对同一目标目录重试续传。
旧 rc20 的 fetch 可以完成签名库存下载；升级操作必须使用新包内的 ccheck。

也提供 `release-@@VERSION@@-patch.tar.gz` 供离线传输。该压缩包有意不含未变化的推理/基础镜像分片，
不能只解压后就跳过完整包校验。须从完整、已验签的旧包补齐新签名清单中 reused_files 指定且哈希一致的原始分片，
再运行新 ccheck package-verify。不要用重新 docker save 生成的分片冒充原分片，不要覆盖旧发布目录。

## 更新与确认

```bash
sudo "$NEW/ccheck" update \
  --config "$SITE" --release "$NEW" --version @@VERSION@@ \
  --trusted-key "$TRUST" --backup-key "$BACKUP_KEY"
sudo "$NEW/ccheck" status --config "$SITE"
```

更新会校验基线、签名、模型与迁移，创建加密备份后才执行数据库升级。任何错误先保留现场与日志，
不要删除 owner、维护或恢复标记来绕过检查，也不要在已有实例上运行 install 来代替 update。

确认 Web 可登录、旧审查/报告仍可见、模型仍为 Qwen2.5 FP8，随后用小任务验收审查与报告。
助手启用后验证闲时聊天与知识文件；审查排队/运行期间会禁用发送和上传，并打断正在生成的聊天。
已上传的知识文件和会话按平台用户隔离。GPU 抢占与异常恢复说明见 ASSISTANT.md。

审查 CLI 的 10 条子命令保持不变；原来已安装 ccheck-review 时无需重新登录或重新安装。

## 可选远程 SSH

阅读 REMOTE-SSH.md，配置独立 terminal.data_dir、IP、端口、HTTPS URL、证书与经过核实的 SSH 主机公钥。
需要跨机器访问时必须配置匹配的 TLS 证书；默认仅回环地址、自签名测试证书。
确认配置后执行：

```bash
sudo "$NEW/ccheck" terminal-install --config "$SITE" --release "$NEW" --trusted-key "$TRUST"
sudo "$NEW/ccheck" terminal-start --config "$SITE"
sudo "$NEW/ccheck" terminal-status --config "$SITE"
```

terminal-stop 只停止终端，保留独立数据库和账号；不会停止审查或模型服务。
终端初始账号 terminal-admin 使用随机密码，与平台账号不同。SSH 密码在连接时输入，不写进站点配置。
默认部署时下载四个固定摘要的开源组件镜像；支持保留摘要的镜像源及预先导入镜像后的 offline 模式。
终端数据不属于审查实例备份，须按 REMOTE-SSH.md 独立备份。

## 恢复与验收边界

本次有新增数据库迁移，不能仅切回旧镜像或尝试自动降级数据库。
需要退回 rc20 时，保留故障现场并使用升级前的加密一致性备份、原备份密钥和配套旧发布物恢复。
缺少旧密钥时不能解密旧备份；不要覆盖凭据或重新初始化账号。

编译服务器完成 A100 40 GiB 隔离验收，不代表目标 RTX5880 已完成安装、吞吐率或准确率验收。
终端为 CPU 功能，但审查、对话和 Jina 嵌入仍需要原有 GPU 环境。
模型及第三方组件许可见随包 notices，安装成功不代表获得商业授权。
