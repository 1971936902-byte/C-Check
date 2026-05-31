# C-Check 部署验收清单

## 1. 静态检查

发布前在仓库根目录执行：

```bash
bash -n start.sh
bash -n deploy/vllm/start-vllm.sh
docker compose config --quiet
```

`docker compose config --quiet` 需要已创建并填写 `.env`，不会启动容器。

## 2. 基础服务

迁移后执行：

```bash
./start.sh status
curl -fsS http://127.0.0.1/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<initial-password>"}'
```

验收项：

- [ ] `mysql`、`redis`、`api`、`worker`、`frontend` 均为运行状态。
- [ ] `mysql`、`redis`、`api`、`worker`、`frontend` 健康检查通过。
- [ ] 首页可访问，刷新前端路由不会出现 Nginx 404。
- [ ] `/api/` 请求由 Nginx 转发到 FastAPI。
- [ ] 初始管理员可登录，并已修改首次密码。

## 3. 模型节点

真实 VLLM 节点检查：

```bash
curl -fsS \
  -H "Authorization: Bearer <VLLM_API_KEY>" \
  http://<model-server-ip>:8000/v1/models
```

验收项：

- [ ] GPU 服务器上的 VLLM 容器为运行状态。
- [ ] `/v1/models` 返回注册时使用的 `model_identifier`。
- [ ] 管理员模型健康检查返回成功。
- [ ] 生产 `.env` 中 `MOCK_MODEL_ENABLED=false`。

## 4. 审查闭环

验收项：

- [ ] 粘贴一段 `.c` 代码创建审查任务，任务从 `queued` 进入 `running`。
- [ ] worker 日志出现任务处理记录。
- [ ] 任务最终进入 `completed`，报告可在线预览。
- [ ] Markdown 与 PDF 报告均可下载。
- [ ] 单个 `.c` 或 `.h` 文件上传可创建任务。
- [ ] 包含 `.c` 与 `.h` 文件的 ZIP 上传可创建任务。
- [ ] 历史报告可筛选、再次下载与删除。

## 5. 权限与失败路径

验收项：

- [ ] 普通用户无法访问 `/api/admin/*`。
- [ ] 普通用户无法读取或删除其他用户的任务。
- [ ] 禁用用户后，该用户无法继续登录。
- [ ] 禁用模型节点后，该节点无法再用于新审查。
- [ ] VLLM 停止后，任务失败且任务详情展示错误，不生成报告，不会伪造成功结果。
- [ ] 将 `.env` 恢复成任意 `CHANGE_ME` 占位值时，`./start.sh start` 明确拒绝启动。

## 6. 升级后检查

验收项：

- [ ] API 日志显示 Alembic 迁移成功。
- [ ] 既有用户仍可登录。
- [ ] 既有历史报告仍可预览和下载。
- [ ] 上传卷数据仍存在。
- [ ] 新建审查任务可由 worker 正常处理。

## 7. Windows 工程生成环境验证记录

本仓库在 Windows 工程生成环境中完成了以下检查：

- [x] `python -m compileall -q backend/app backend/alembic`
- [x] `git diff --check`
- [x] `cd frontend && npm run typecheck`
- [x] `cd frontend && npm run build`
- [x] `cd frontend && npm audit --omit=dev --audit-level=high`
- [x] 使用 Vite 静态预览检查桌面端和移动端登录页渲染

以下项目必须迁移到 Linux 服务器后执行：

- [ ] `bash -n start.sh`
- [ ] `bash -n deploy/vllm/start-vllm.sh`
- [ ] `docker compose config --quiet`
- [ ] MySQL、Redis、Celery、Nginx 与 FastAPI 联调
- [ ] Alembic 迁移执行
- [ ] 真实 VLLM 节点推理、超时和严格失败验证
- [ ] 完整后端自动化测试
