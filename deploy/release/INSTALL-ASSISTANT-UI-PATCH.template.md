# C-Check @@VERSION@@ AI 助手界面补丁

## 适用范围

从已正常部署、完整验签的 **v0.1.0-rc.22 / Qwen2.5-Coder-32B FP8** 升级。
本包不是 rc20/rc21 到 rc22 的替代升级包；仍在更早版本时先按原发布说明升级到 rc22。
不支持 A3B/Qwen3 基线，不更换模型、Jina 权重、后端、数据库结构、推理镜像、性能配置或审查 CLI。

本次仅替换前端镜像：助手左侧、用户右侧；安全 Markdown 与代码高亮、代码单独复制；
阅读时不被流式回复拉走、底部新内容提示；等待时长与基于实际响应的状态；手动重试和重新生成。
重新生成会在原会话发送一个新请求，保留旧回答与未发送草稿，不自动重复提交；审查仍具有最高优先级。
后端没有独立检索阶段事件时，页面合并显示等待检索与模型响应，不提供虚构进度。

原发布者公钥文件 SHA256：`@@PUBLISHER_PEM_SHA256@@`。
请使用独立保存的原可信公钥，不要修改签名清单或跳过校验。rc22 已授权的 GPU 启动脚本原样保留。

## 获取并校验

以下 OLD 必须是完整、已验签的 rc22 发布目录，不是缺少镜像分片的补丁目录。

```bash
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.22
DEST=/home/lyh/c-check-packages
NEW="$DEST/release-@@VERSION@@"
SITE=/home/lyh/c-check-site.json
TRUST=/home/lyh/c-check-instance/config/publisher.pem
BACKUP_KEY=/home/lyh/c-check-secrets/backup.key

sudo "$OLD/ccheck" package-verify --release "$OLD" --trusted-key "$TRUST"
sudo "$OLD/ccheck" fetch --version @@VERSION@@ \
  --base-url https://github.com/1971936902-byte/C-Check/releases/download \
  --destination "$DEST" --trusted-key "$TRUST" --reuse-release "$OLD"
sudo "$OLD/ccheck" package-verify --release "$NEW" --trusted-key "$TRUST"
sudo "$NEW/ccheck" package-verify --release "$NEW" --trusted-key "$TRUST"
```

也提供 `release-@@VERSION@@-patch.tar.gz` 与 `.sha256`。离线部署时，在独立新目录解压，
从完整已验签 rc22 目录补齐新清单 reused_files 中指定的原始镜像分片，再用可信旧安装器完整验签，
通过后才能执行新包代码。不要覆盖旧发布目录，不要用重新 docker save 的文件替换原分片。
本补丁不含未变化的后端、AnythingLLM、Qwen、Jina、MySQL、Redis、Qdrant 镜像分片或模型权重。

## 升级

先留维护窗口，结束在途审查和对话，保留现有配置、实例、数据库、旧包及原备份密钥。
虽然不迁移数据库结构，正式 update 仍会维护停机、执行加密一致性备份并按安装器流程重启相关服务。
不要在现有实例上运行 install，不要绕过维护、网络、模型或签名检查。

```bash
sudo "$NEW/ccheck" update --config "$SITE" --release "$NEW" \
  --version @@VERSION@@ --trusted-key "$TRUST" --backup-key "$BACKUP_KEY"
sudo "$NEW/ccheck" status --config "$SITE"
```

确认页面版本后强制刷新，检查旧报告、会话及知识文件仍可访问，随后用小对话验证代码复制、
流式输出、停止、重试和审查忙碌禁用状态。等待时长不是预计剩余时间。
保留旧 rc22 发布物和备份，发生异常先保存日志，不删除业务数据或重新初始化账号。

## 验收边界

本次在 CPU 环境执行前端测试与构建、非 root 只读 HTTP 检查、镜像分层审计、
桌面/平板/手机浏览器 API 模拟测试，以及最终包验签、GPU 配方与助手授权检查。
没有启动 GPU 推理、没有升级编译服务器业务实例，也没有重复宣称真实 GPU 抢占验收。
