# AI 智能助手部署与验收

## 当前交付状态

本目录是 AI 智能助手的开发实现和验收工具，不是已签名的补丁包。原有 rc20 发布目录保持不变；旧版安装器不能仅通过增加配置字段启用本功能。部署需要包含本目录运行资源、AnythingLLM 镜像、四个新增数据库迁移以及对应签名元数据的新版本。

助手复用现有 Qwen2.5-Coder-32B FP8 和 GPU Jina，不切换 Qwen3/A3B，也不额外加载一份模型。AnythingLLM 提供内部知识库能力，用户通过 C-Check 页面访问；不对外开放 AnythingLLM 管理端口，禁用其代理和工具能力。

## 使用与调度

- 从侧边栏进入“AI智能助手”，创建对话、上传知识文件并发送消息。
- 文件支持 `.txt`、`.md`、`.pdf`、`.docx`、`.csv`、`.json`、`.c` 和 `.h`；每批最多 20 个、单个最多 10 MiB、每批合计最多 40 MiB。
- 对话及文件按用户隔离。上传完成不等于入库完成，文件状态就绪后才能用于检索。
- 审查优先：审查排队或运行时停止接收新的聊天与入库工作，正在生成的聊天会被中断。知识图谱等模型关联工作也参与调度。
- 忙碌时仍可读取历史。停止生成或被抢占后，需要确认底层推理已停止，才能释放计算占用；网页断开不作为 GPU 空闲证明。
- 异常中断的文件可以在恢复后重试。系统不会自动续写被打断的聊天。

## 启用配置

在包含助手功能的新发布包中，保留站点原有实例、模型、GPU、网络和 Web 配置，增加：

```json
{
  "performance": {},
  "assistant": {
    "enabled": true,
    "subnet": "172.30.10.192/26"
  }
}
```

助手网络必须是未被现有 Docker 网络和宿主机路由占用的私有网段。生产配方要求已有 Qwen2.5 FP8 与 GPU Jina 的 `ada-48g-fp8` 加速配置；不要直接把测试用 A100 参数复制到部署环境。

升级前使用现有备份流程生成加密备份，妥善保管外部备份密钥。数据库新增迁移为 `0018` 至 `0021`；安装器会校验其内容、原版本及签名授权。不能通过更换旧镜像来回退数据库，应恢复与旧版本匹配的备份。

首次启用会从原有三个网络增加到四个网络，需要按冷网络迁移升级。确认新发布包已正式签名、外部配置已包含以上字段后，例如：

```bash
sudo /path/to/new-release/ccheck update \
  --config /home/lyh/c-check-site.json \
  --release /path/to/new-release \
  --trusted-key /home/lyh/c-check-instance/config/publisher.pem \
  --backup-key /path/to/existing/backup.key \
  --cold-network-migration
```

将占位目录和备份密钥路径替换为实际值，继续使用现有外部备份密钥，不要覆盖它。冷迁移会暂停本实例服务；若助手网络已经存在且网络配方没有变化，不应再传冷迁移选项。不要直接编辑实例内部的 `config/site.json` 绕过配置哈希校验，后续开关变更也通过安装器的升级流程完成。

新增内部服务为 `assistant-gateway`、`anythingllm` 和 `assistant-worker`。运行资源使用内容寻址和哈希校验；配置密钥不下发浏览器。助手存储目录要求所属运行用户可访问且组和其他用户不可访问，密钥及提供方环境文件要求 `0600`。不要通过 `chmod -R 777` 解决权限错误。

## 状态与日志

以下命令使用新版本发布目录中的 `ccheck`，不是 `ccheck-review`。将 `/path/to/new-release` 替换为实际目录：

```bash
sudo /path/to/new-release/ccheck status --config /home/lyh/c-check-site.json
sudo /path/to/new-release/ccheck logs assistant-gateway --config /home/lyh/c-check-site.json --tail 100
sudo /path/to/new-release/ccheck logs assistant-worker --config /home/lyh/c-check-site.json --tail 100
sudo /path/to/new-release/ccheck logs anythingllm --config /home/lyh/c-check-site.json --tail 100
```

备份包含上传原文件及 `data/assistant` 中的提供方知识库存储。日志、备份和恢复记录可能涉及私有数据，不应直接公开上传。

## 异常恢复

若模型进程重启或连接中断造成无法证明旧任务已停止，系统会保守阻止助手继续执行。先查看日志，确认没有正在运行的审查或图谱任务，再执行：

```bash
sudo /path/to/new-release/ccheck assistant-recover --config /home/lyh/c-check-site.json
```

恢复工具会拒绝中断正在运行的审查或图谱任务。获准恢复后，它会封锁新请求、停止助手写入者、重建本实例管理的 Qwen 和 Jina 容器，等待健康检查，再重启内部助手服务并清理旧调度占用。此过程会短暂停止模型服务，不会删除模型权重、数据库或知识库。

恢复记录位于实例的 `state/assistant-recovery.json`。中途失败会保留维护状态和恢复记录；排除原因后使用同一命令继续，不要手工删除数据库调度标记或恢复记录。未完成恢复时备份和升级会被阻止。恢复成功后可在页面重试被暂停的文件，排队审查按调度继续。

## 构建与验收边界

AnythingLLM 固定提交、许可哈希和补丁列表见 `anythingllm.lock.json`；Prisma、libvips 的校验值见相应锁文件。镜像保留上游 MIT 许可，路径为 `/usr/share/licenses/anythingllm/LICENSE`。构建器校验上游提交、补丁和下载内容，不跟随浮动最新版。

安装器应使用 `deploy/release/build_cli.py --with-assistant` 构建，并把对应资源哈希、迁移授权及不可变镜像标识写入新发布清单。只生成 zipapp 不等于完成签名发布；源码开发镜像也不能冒充生产编译镜像。普通构建和静态测试不依赖 GPU，真实推理及抢占验收仍需要 GPU。

已在隔离的 A100 40 GiB 测试实例验证真实 Qwen/Jina、批量入库、RAG 引用、用户隔离、审查抢占、报告生成、浏览器交互及异常恢复。测试使用专用显存参数，并在结束后恢复原推理容器。这不能替代目标 48 GiB 部署验收，也不代表准确率或吞吐率承诺。

主要工具：`probe_anythingllm_image.py` 验证内部提供方；`probe_deployment_recipe.py` 验证安装配方、权限和篡改检测；`probe_compose_stack.py` 验证隔离的完整业务栈。后者会临时停止指定的原推理容器，必须事先获准并确认恢复方案。

### 编译镜像候选

`build_backend_candidate.py` 可以复用已签名 rc19/rc20 的编译核心，在其上加入助手的允许公开模块与迁移。它会验证发布签名、独立指定的发布者指纹、基线源码、依赖、模型身份、每个原生模块哈希及所有镜像层。如果核心代码或依赖变化则拒绝复用，需要完整可信编译。

源码先通过 `deploy/release/pack_source.py` 打包。该清单包含 `deploy/assistant`；开发快照对未修改文件使用 Git 的标准字节，避免 Windows CRLF 换行被误认成核心代码变更。真正修改的文件保持工作区内容。`--working-tree` 快照只供开发验收，不能作为稳定签名发布来源。

在 Linux 构建机解包源码、载入已签名基线镜像后，例如：

```bash
python3 source/deploy/assistant/build_backend_candidate.py \
  --source ./source \
  --base-source /path/to/verified-rc20-source \
  --base-release /path/to/release-v0.1.0-rc.20 \
  --trusted-key /path/to/trusted/publisher.pem \
  --publisher-sha256 <independently-verified-public-key-sha256> \
  --output ./new-candidate-build \
  --tag ccheck-assistant-backend:native-candidate
```

只有明确使用开发快照时才增加 `--allow-worktree-snapshot`。候选构建目录必须不存在，输出 `candidate.json` 明确标记为未签名且非可发布版本。

`probe_native_parity.py --source ./source --image <image-id> --output ./new-parity-results` 在禁用网络的容器中运行相同的源码/编译后端测试，比较测试清单和结果。完整 GPU 验收可向 `probe_compose_stack.py` 增加 `--compiled-backend --backend-image <image-id> --provider-image <image-id>`，从而使用正式 `/app/backend` 布局，而不是开发镜像的源码目录。通过候选验收仍不等于完成版本分配、签名和发布。

### 正式发布入口

通用发布器 `deploy/release/build_release.py` 提供显式 `--with-assistant` 选项。必须同时提供已验证的 rc19/rc20 基线与信任公钥，并沿用原发布者身份；源码必须是提交后的冻结清单，拒绝开发快照。沿用通用发布流程的镜像、依赖锁、版本与验收报告输入，不应直接把候选目录当作发布上下文。

报告除原有版本、源码树与全部镜像绑定外，必须具有 `assistant` 对象，且 `passed`、`real_gpu`、`review_preemption`、`rag`、`owner_isolation`、`recovery`、`source_native_parity` 全部为 `true`。这些值应来自对应验收记录，不能为了通过发布门禁而手填成功。

发布器把助手运行资源和迁移嵌入安装器，检查提供方镜像中的 MIT 许可，输出 `ASSISTANT.md`、许可及组件锁文件，并在签名清单中记录运行资源哈希和需备份的四项迁移授权。模型清单变化或未知迁移会被拒绝。新版本号和实际签名发布仍应单独确认；本目录不指定或覆盖 rc20。

助手组包保留基线中已签名的 `ccheck-review`、CLI 文档与终端文件。Qwen 依赖清单不变时，可复用已签名且逐项校验一致的 wheel 下载来源；不会因为只更新入口脚本而重新查询 PyPI。新依赖或哈希变化仍需重新核实来源。

`probe_upgrade.py` 使用独立 MySQL 验证原 rc20 数据保留和新增助手表；`probe_release_pipeline.py` 用自动销毁的临时 Ed25519 密钥验证实际组包、篡改拒绝、内嵌资源、既有 CLI/终端保留及助手文件加密快照往返。后者需要 Linux、已缓存的锁定依赖和足够的临时空间，默认使用 `/dev/shm` 避免挤占磁盘。测试版本 `rc.999` 仅是隔离夹具，不是新的正式版本，也不会自动发布。
