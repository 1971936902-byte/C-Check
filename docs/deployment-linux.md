# C-Check Linux 部署指南

## 1. 架构

主站使用 Docker Compose 启动五个服务：

| 服务 | 用途 |
| --- | --- |
| `mysql` | MySQL 8.0，保存用户、模型节点、任务与报告 |
| `redis` | Celery broker 与结果后端，开启 AOF |
| `api` | FastAPI；每次启动先执行 `alembic upgrade head` |
| `worker` | Celery 审查任务消费者 |
| `frontend` | Nginx 静态站点，同时将 `/api/` 代理到 API |

模型推理服务与主站解耦。每台 GPU Linux 服务器可用
`deploy/vllm/start-vllm.sh` 独立启动一个 OpenAI 兼容 VLLM 节点，再由管理员注册。

## 2. 首次启动

要求：Linux、Docker Engine、Docker Compose 插件。首次部署执行：

```bash
git clone <repository-url> c-check
cd c-check
chmod +x start.sh deploy/vllm/start-vllm.sh
./start.sh install
```

编辑 `.env`，替换全部 `CHANGE_ME`。数据库密码、数据库 root 密码、JWT 密钥和管理员密码必须不同且足够长。生产环境保持：

```dotenv
MOCK_MODEL_ENABLED=false
ALLOW_INSECURE_DEFAULTS=false
```

启动并查看状态：

```bash
./start.sh start
./start.sh status
./start.sh logs api worker
```

浏览器访问 `http://<server-ip>:<WEB_PORT>`。首次登录用户名为 `.env` 的
`ADMIN_USERNAME`，密码为 `ADMIN_PASSWORD`。登录后立即在个人密码修改页面变更管理员密码；
也可调用 `POST /api/auth/password` 完成修改。

## 3. 注册真实 VLLM 节点

在具备 NVIDIA GPU、驱动和 NVIDIA Container Toolkit 的模型服务器上：

```bash
cd deploy/vllm
cp .env.example .env
vim .env
chmod +x start-vllm.sh
./start-vllm.sh
```

`MODEL_ID` 填 Hugging Face 模型仓库，例如实际采用的 Qwen3-Coder-30B 仓库；
`SERVED_MODEL_NAME` 是平台注册时使用的模型标识。生产环境应将 `VLLM_IMAGE`
从 `latest` 固定为已验收版本或镜像 digest。

检查节点：

```bash
curl -fsS \
  -H "Authorization: Bearer <VLLM_API_KEY>" \
  http://<model-server-ip>:8000/v1/models
```

管理员登录主站后，在模型管理中新增节点：

```json
{
  "display_name": "Qwen3 Coder 30B",
  "model_identifier": "qwen3-coder-30b",
  "base_url": "http://<model-server-ip>:8000",
  "api_key": "<VLLM_API_KEY>",
  "timeout_seconds": 600,
  "is_enabled": true,
  "description": "production GPU node"
}
```

对应接口为 `POST /api/admin/models`。注册后调用
`POST /api/models/<model-id>/health` 验证主站到模型节点的网络与鉴权。

多台模型服务器重复以上步骤并分别注册。模型节点独立启停，不需要修改主站镜像。

## 4. Mock 开发模式

Mock 只用于隔离开发环境。将主站 `.env` 设置为：

```dotenv
MOCK_MODEL_ENABLED=true
```

然后注册模型节点：

```json
{
  "display_name": "Local Mock",
  "model_identifier": "mock",
  "base_url": "mock://local",
  "timeout_seconds": 30,
  "is_enabled": true,
  "description": "isolated development only"
}
```

生产环境必须保持 `MOCK_MODEL_ENABLED=false`。否则测试节点可能被误用为真实审查能力。

## 5. 严格失败策略

平台默认拒绝弱配置：

- `./start.sh start` 在 `.env` 缺失或包含 `CHANGE_ME` 时直接退出。
- Compose 在必填变量缺失时直接退出。
- FastAPI 在数据库密码过短、JWT 密钥不足 32 字符、管理员密码不足 12 字符时拒绝启动。
- API 启动时 Alembic 迁移失败则不会启动 Uvicorn。
- VLLM 脚本在配置缺失、占位值未替换、同名容器已存在时直接退出。

不要在生产环境将 `ALLOW_INSECURE_DEFAULTS` 改为 `true`。

## 6. 停止、备份与升级

停止服务：

```bash
./start.sh stop
```

升级前建议保持服务运行，先备份数据库、上传卷和配置：

```bash
mkdir -p backups
docker compose exec -T mysql sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction "$MYSQL_DATABASE"' \
  > "backups/c-check-$(date +%F-%H%M%S).sql"
docker run --rm \
  -v c-code-review-platform_uploads_data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine sh -c 'tar czf /backup/uploads-$(date +%F-%H%M%S).tgz -C /data .'
cp .env "backups/env-$(date +%F-%H%M%S).backup"
```

Compose 卷名称会受项目目录名影响。执行 `docker volume ls` 核对实际上传卷名称，
再替换命令中的 `c-code-review-platform_uploads_data`。

拉取代码并升级：

```bash
git pull --ff-only
./start.sh start
./start.sh status
```

`api` 容器会在提供流量前执行数据库迁移。升级后按
[`docs/verification.md`](verification.md) 完成验收。

## 7. 常用运维命令

```bash
./start.sh status
./start.sh logs
./start.sh logs api
./start.sh logs worker
docker compose exec mysql mysql -uc_check -p c_check
docker logs -f c-check-vllm-qwen3-coder
```
