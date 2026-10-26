# C-Check @@VERSION@@ 应用增量补丁

## 适用范围

仅从完整、已正常部署的 **v0.1.0-rc.16 / Qwen2.5-Coder-32B-Instruct-FP8** 升级。
rc.13/14/15 先按原说明升级至 rc.16；本补丁不适用于 rc.17/Qwen3，不会替换模型。

本补丁包含 TXT 源码输入支持、前端显示/跳转/排序/刷新修复，以及手动生成图谱。
审查结束后不再自动整理全量补充图谱；在后台管理的 RAG 图谱页点击“生成图谱”才提交后台任务。
审查需要的 RAG 定义检索仍保留。报告页移除“审查补充信息”卡片。

仅 backend、frontend 镜像变化。Qwen、Jina、MySQL、Redis、Qdrant 镜像，模型文件清单、依赖锁、
GPU 启动器和核心编译模块均从 rc.16 复用。沿用站点配置，不传模型迁移或性能配置切换参数。
签名公钥文件 SHA256：`@@PUBLISHER_PEM_SHA256@@`。
签名私钥不在包中；请使用此前独立保存的可信公钥，不要把同一个下载源中的公钥直接当信任依据。

## 升级前

1. 确认当前版本为 rc.16，保留完整旧发布目录和原站点配置。
2. 留出本项目维护窗口，等待审查/图谱任务结束。升级会短暂停止本项目服务，不重启 Docker、不操作其他项目。
3. 保留现有账号、密码、数据库、上传文件、模型路径、网络网段和密钥；不要重新执行初始化、删除 data 目录或执行 `down -v`。
4. 准备原来的备份加密密钥，必须位于实例目录之外、权限 0600、内容为单行十六进制文本。
   若从未创建，可在一个不存在的新路径执行 `umask 077; openssl rand -hex 48 > /安全目录/ccheck-backup.key`。
   不要覆盖已有备份密钥；它不是发布签名私钥。
5. 留足空间：除新发布包/镜像外，备份至少需要实例数据大小、文件开销和 3GiB 安全余量。

## 增量下载

按实际路径修改变量。`OLD` 必须是包含已验证原始镜像分片的完整 rc.16 包，不是只有几个附件的 patch 文件夹。

```bash
OLD=/home/lyh/c-check-packages/release-v0.1.0-rc.16
DEST=/home/lyh/c-check-packages
NEW="$DEST/release-@@VERSION@@"
TRUST=/home/lyh/c-check-trust/publisher.pem
SITE=/home/lyh/c-check-site.json
BACKUP_KEY=/home/lyh/c-check-secrets/backup.key

sudo "$OLD/ccheck" fetch \
  --version @@VERSION@@ \
  --base-url https://github.com/1971936902-byte/C-Check/releases/download \
  --destination "$DEST" --trusted-key "$TRUST" --reuse-release "$OLD"

sudo "$NEW/ccheck" package-verify --release "$NEW" --trusted-key "$TRUST"
```

fetch 验证签名及逐文件哈希，复用旧包中未变化的镜像分片，只下载新引导包和 backend/frontend 分片。
没有完整旧包时仍可下载，但缺失的复用分片会从原 Release 获取，流量和磁盘占用更大。
不下载模型权重。不要修改新包内任何签名文件，不要用重新 docker save 的文件冒充旧分片。

## 更新

```bash
sudo "$NEW/ccheck" update \
  --config "$SITE" --release "$NEW" --version @@VERSION@@ \
  --trusted-key "$TRUST" --backup-key "$BACKUP_KEY"

sudo "$NEW/ccheck" version --config "$SITE"
sudo "$NEW/ccheck" status --config "$SITE"
sudo "$NEW/ccheck" verify --config "$SITE"
```

使用新包的 CLI，不要使用 rc.16 CLI 执行 update。不要添加 `--model-migration` 或切换性能配置的参数。
旧登录账户和密码保留，不会恢复默认密码。

## 数据库与失败处理

新增迁移 `0016_manual_graph_build` 仅添加图谱状态、构建 ID、错误信息字段；不删除旧报告或索引。
部署器只认可签名绑定的 rc.16 -> 本版本迁移，先做加密备份，再执行迁移和验收。
未知版本迁移继续拒绝。

**数据库跨版本不支持直接 image rollback。** 迁移或验收失败时保持维护状态，停止 API/worker，
不会把旧镜像直接接到新版本数据库上。保留 update 输出的 ENCRYPTED_BACKUP 路径、密钥、旧发布包和日志，
按匹配 rc.16 状态及镜像的完整备份恢复流程恢复；不要手工改 schema_epoch、删 alembic_version 或反复运行 install。
恢复会丢弃备份之后新增的数据，必须先确认恢复范围。

## 验收

- 历史报告、账号、上传文件保留，模型仍为 Qwen2.5，Jina 路径未变化。
- 上传 `xxx_c.txt` / `xxx_h.txt` 可识别对应 C 源码。
- 审查正常完成，报告没有“审查补充信息”卡片。
- 打开/刷新图谱页不创建任务；点击“生成图谱”后可见排队、生成、完成或失败状态。
- 图谱任务复用现有 worker，其他任务繁忙时会排队，关闭页面不会取消后台任务。

此次应用补丁不会提升模型能力，也不包含尚未实施的格式化推理、分片续跑或向量索引拆分优化。
构建证据见 `verify/build-gates.json`；未重新开展 RTX5880 真机或准确率评估。
