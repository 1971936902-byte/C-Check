# C-Check 云服务器前后端部署实战记录

日期：2026-06-04  
适用范围：Ubuntu 22.04 云服务器，前后端分离部署，FastAPI + Celery + Redis + MySQL + Nginx + Vue3/Vite。

本文记录本次 C-Check 在云服务器上的完整部署步骤、验证方式、踩过的坑和最终成功经验。文档中的密码、Token、数据库密钥请使用你自己的安全值，不要把真实密钥提交到 Git。

## 1. 最终访问架构

本次成功方案不是直接访问服务器的 80 端口，而是使用云平台提供的“预留端口映射”。

```text
公网访问：
http://180.127.11.167:24164/
http://223.109.239.30:24164/

云平台端口映射：
外网 24164 -> 服务器内网 8800

服务器内部：
Nginx 监听 0.0.0.0:8800
Nginx /api/ 反向代理到 127.0.0.1:8000
FastAPI 监听 127.0.0.1:8000
Celery worker 连接 Redis
MySQL 保存用户、模型、任务、报告
Redis 作为 Celery broker/result backend
```

服务拆分：

| 服务 | 端口/位置 | 作用 |
| --- | --- | --- |
| Nginx | `0.0.0.0:8800` | 对外提供前端静态页面，并代理 `/api/` |
| FastAPI | `127.0.0.1:8000` | 后端 API |
| Celery worker | 无 HTTP 端口 | 执行代码审查异步任务 |
| MySQL | `127.0.0.1:3306` | 持久化业务数据 |
| Redis | `127.0.0.1:6379` | Celery 队列和结果后端 |

## 2. 前置连接信息

以新服务器为例：

```bash
ssh root@180.127.11.167 -p 24116
```

如果电信公网不可用，可尝试移动公网：

```bash
ssh root@223.109.239.30 -p 24116
```

建议优先使用 `root` 做首次部署，部署完成后再根据安全需求创建普通运维用户。

## 3. 系统环境检查

登录服务器后先确认系统、GPU、磁盘和端口状态：

```bash
hostname
whoami
cat /etc/os-release
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
df -h /
ss -ltnp
```

本次成功环境：

```text
Ubuntu 22.04.3 LTS
NVIDIA A100-SXM4-40GB
根分区约 196G，总可用约 147G
```

注意：当前可用部署先使用 `mock://local` 模型节点跑通业务闭环，真实 30B+ VLLM 模型可以后续再接入。

## 4. 准备基础软件

如果服务器已经有 MySQL、Redis、Nginx、Python 3.12、Node 22，可以跳过重复安装。检查命令：

```bash
systemctl status mysql redis-server nginx --no-pager
python3 --version
/opt/miniconda/bin/python --version || true
/opt/node22/bin/node -v || true
```

如果缺少组件，可按下面方式安装。

### 4.1 安装系统包

```bash
apt update
apt install -y \
  git curl xz-utils build-essential nginx redis-server mysql-server \
  pkg-config default-libmysqlclient-dev libssl-dev libffi-dev \
  python3-venv python3-dev
```

启动基础服务：

```bash
systemctl enable --now mysql redis-server nginx
```

### 4.2 安装 Python 3.12

本次使用 Miniconda 提供 Python 3.12：

```bash
bash /root/Miniconda3-py312_24.9.2-0-Linux-x86_64.sh -b -p /opt/miniconda
/opt/miniconda/bin/python --version
```

如果服务器没有安装包，可从清华源、阿里源或 Miniconda 官网下载对应 Linux x86_64 安装脚本。

### 4.3 安装 Node 22

```bash
cd /opt
curl -fsSL -o node.tar.xz https://npmmirror.com/mirrors/node/v22.13.1/node-v22.13.1-linux-x64.tar.xz
mkdir -p /opt/node22
tar -xJf node.tar.xz --strip-components=1 -C /opt/node22
/opt/node22/bin/node -v
/opt/node22/bin/npm -v
```

后续构建前端时记得设置：

```bash
export PATH=/opt/node22/bin:$PATH
```

## 5. 获取代码

```bash
cd /opt
git clone https://github.com/1971936902-byte/C-Check.git c-check
cd /opt/c-check
git checkout master
git pull --ff-only origin master
```

如果服务器已经存在 `/opt/c-check`：

```bash
cd /opt/c-check
git fetch origin master
git reset --hard origin/master
git rev-parse --short HEAD
```

本次最终部署提交：

```text
1ed0f7e fix: return validation error for unsupported check types
```

## 6. 配置 .env

在仓库根目录创建 `/opt/c-check/.env`。示例：

```dotenv
MYSQL_DATABASE=c_check
MYSQL_USER=c_check
MYSQL_PASSWORD=<strong_mysql_password>
MYSQL_ROOT_PASSWORD=<strong_mysql_root_password>

DATABASE_URL=mysql+pymysql://c_check:<strong_mysql_password>@127.0.0.1:3306/c_check
REDIS_URL=redis://127.0.0.1:6379/0

JWT_SECRET=<at_least_32_chars_random_secret>
JWT_EXPIRE_MINUTES=480
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<strong_admin_password>

UPLOAD_MAX_FILE_BYTES=1048576
UPLOAD_MAX_ARCHIVE_BYTES=10485760
UPLOAD_MAX_EXTRACTED_BYTES=10485760
UPLOAD_MAX_FILES=200
UPLOAD_MAX_ARCHIVE_ENTRIES=1000
UPLOAD_MAX_PATH_LENGTH=512

CORS_ORIGINS='["http://180.127.11.167:24164","http://223.109.239.30:24164","http://localhost"]'

MOCK_MODEL_ENABLED=true
ALLOW_INSECURE_DEFAULTS=false
WEB_PORT=8800
STORAGE_PATH=/opt/c-check/uploads
```

注意点：

- `JWT_SECRET` 至少 32 字符。
- `ADMIN_PASSWORD` 至少 12 字符。
- 不要在生产环境设置 `ALLOW_INSECURE_DEFAULTS=true`。
- `CORS_ORIGINS` 建议用单引号包住 JSON 数组，避免 shell/systemd 解析破坏 JSON。
- 初次部署为了验证完整流程，可以暂时设置 `MOCK_MODEL_ENABLED=true`。真实模型接入后再改成 `false`。

## 7. 初始化 MySQL 数据库

用 `.env` 中的数据库名、用户名和密码创建数据库：

```bash
mysql -uroot <<'SQL'
CREATE DATABASE IF NOT EXISTS c_check CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'c_check'@'127.0.0.1' IDENTIFIED BY '<strong_mysql_password>';
CREATE USER IF NOT EXISTS 'c_check'@'localhost' IDENTIFIED BY '<strong_mysql_password>';
GRANT ALL PRIVILEGES ON c_check.* TO 'c_check'@'127.0.0.1';
GRANT ALL PRIVILEGES ON c_check.* TO 'c_check'@'localhost';
FLUSH PRIVILEGES;
SQL
```

如果 MySQL 已经有旧数据库，需要先确认是否要保留数据，不要直接 drop。

## 8. 后端部署

创建 Python 虚拟环境：

```bash
cd /opt/c-check
/opt/miniconda/bin/python -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e 'backend[test]'
```

执行数据库迁移：

```bash
cd /opt/c-check/backend
/opt/c-check/.venv/bin/alembic upgrade head
/opt/c-check/.venv/bin/alembic current
```

成功结果应包含：

```text
0003_model_default (head)
```

## 9. 后端测试

生产 `.env` 会影响测试配置，例如 `MOCK_MODEL_ENABLED=true`、生产管理员密码等。跑测试时建议临时移开 `.env`：

```bash
cd /opt/c-check
mv .env .env.runtime
cd backend
/opt/c-check/.venv/bin/python -m pytest tests -q
cd /opt/c-check
mv .env.runtime .env
```

本次最终结果：

```text
79 passed, 4 warnings
```

## 10. systemd 服务配置

### 10.1 FastAPI 服务

写入 `/etc/systemd/system/c-check-api.service`：

```ini
[Unit]
Description=C-Check FastAPI service
After=network.target mysql.service redis-server.service
Wants=mysql.service redis-server.service

[Service]
Type=simple
WorkingDirectory=/opt/c-check/backend
ExecStart=/opt/c-check/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### 10.2 Celery worker 服务

写入 `/etc/systemd/system/c-check-worker.service`：

```ini
[Unit]
Description=C-Check Celery worker
After=network.target redis-server.service mysql.service
Wants=redis-server.service mysql.service

[Service]
Type=simple
WorkingDirectory=/opt/c-check/backend
ExecStart=/opt/c-check/.venv/bin/celery -A app.worker.celery_app worker --loglevel=INFO
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
systemctl daemon-reload
systemctl enable --now c-check-api c-check-worker
systemctl status c-check-api c-check-worker --no-pager
```

注意：这里没有使用 `EnvironmentFile=/opt/c-check/.env`，因为后端配置代码会自动读取仓库根目录 `.env`。这样可以避免 systemd 解析 `CORS_ORIGINS` JSON 字符串时出错。

## 11. 前端构建

```bash
export PATH=/opt/node22/bin:$PATH
cd /opt/c-check/frontend
npm config set registry https://registry.npmmirror.com
npm ci
npm run build
```

构建成功后应存在：

```bash
ls -lah /opt/c-check/frontend/dist
```

Vite 可能提示 chunk 超过 500KB，这是体积优化建议，不影响本次部署运行。

## 12. Nginx 配置

写入 `/etc/nginx/sites-available/c-check`：

```nginx
server {
    listen 80 default_server;
    listen 8800 default_server;
    listen [::]:80 default_server;
    server_name _;

    root /opt/c-check/frontend/dist;
    index index.html;

    client_max_body_size 20m;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /assets/ {
        try_files $uri =404;
        expires 7d;
        add_header Cache-Control "public, max-age=604800";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

启用配置：

```bash
rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/c-check /etc/nginx/sites-enabled/c-check
nginx -t
systemctl reload nginx
```

确认监听：

```bash
ss -ltnp | grep -E ':(8800|8000|80|3306|6379) '
```

成功时应看到：

```text
0.0.0.0:8800  nginx
127.0.0.1:8000 uvicorn
127.0.0.1:3306 mysqld
127.0.0.1:6379 redis-server
```

## 13. 创建 mock 模型节点

初次部署没有真实 VLLM 模型时，先注册 mock 模型节点，保证网站功能可用：

```json
{
  "display_name": "Local Mock Model",
  "model_identifier": "mock-local",
  "base_url": "mock://local",
  "timeout_seconds": 30,
  "is_enabled": true,
  "description": "Deployment smoke-test model. Replace with VLLM endpoint when ready."
}
```

可通过后台页面创建，也可以登录后调用 API：

```bash
curl -X POST http://127.0.0.1:8800/api/admin/models \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Local Mock Model",
    "model_identifier": "mock-local",
    "base_url": "mock://local",
    "timeout_seconds": 30,
    "is_enabled": true,
    "description": "Deployment smoke-test model. Replace with VLLM endpoint when ready."
  }'
```

健康检查：

```bash
curl -X POST http://127.0.0.1:8800/api/models/<model_id>/health \
  -H "Authorization: Bearer <admin_token>"
```

应返回：

```json
{"ok": true, "kind": "mock"}
```

## 14. 本地与公网验证

### 14.1 服务器本地验证

```bash
curl -i http://127.0.0.1:8800/
curl -i -X POST http://127.0.0.1:8800/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<admin_password>"}'
```

### 14.2 公网验证

根据云平台映射：

```text
外网 24164 -> 内网 8800
```

浏览器访问：

```text
http://180.127.11.167:24164/
http://223.109.239.30:24164/
```

命令行验证建议绕过本机代理：

```bash
curl --noproxy "*" -i http://180.127.11.167:24164/
```

成功时返回：

```text
HTTP/1.1 200 OK
<title>C-Check · C 语言智能代码审查</title>
```

端到端验证内容：

1. 登录管理员账号。
2. 获取模型列表，确认存在 `Local Mock Model`。
3. 提交一段 C 代码。
4. 轮询任务状态。
5. 任务完成后读取报告。

本次公网端到端验证结果：

```text
public_login 200 True
public_models 200 1 Local Mock Model
public_task_created 201 queued
poll 1 completed 100
public_report 200 100.0 Mock review completed for 1 source file(s).
```

## 15. 踩过的坑与解决经验

### 15.1 公网 IP:端口不是自动直通服务器端口

现象：

- 服务器内部 `curl http://127.0.0.1:18000/` 正常。
- Windows 访问公网 `180.127.x.x:18000` 返回 502 或非标准响应。
- Nginx access log 没有外部请求记录。

原因：

云平台使用 NAT/网关，外网端口必须按“预留端口映射”转发到服务器内网端口。不能假设公网端口和服务器监听端口一致。

解决：

查看云平台预留端口，例如：

```text
外网 24164 -> 内网 8800
```

让 Nginx 监听服务器内网 `8800`，然后访问：

```text
http://公网IP:24164/
```

### 15.2 本机代理会干扰公网测试

现象：

PowerShell `Invoke-WebRequest` 返回 502，但 TCP 端口是 open。

原因：

本机设置了：

```text
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
```

请求先经过本机代理，502 可能来自代理而不是云服务器。

解决：

用 curl 绕过代理：

```bash
curl --noproxy "*" -i http://180.127.11.167:24164/
```

### 15.3 MySQL 非事务 DDL 导致 Alembic 半完成

现象：

第一次迁移失败后，`is_default` 字段已经添加，但 Alembic 版本未记录。再次执行时报：

```text
Duplicate column name 'is_default'
```

原因：

MySQL DDL 非事务化，部分 DDL 可能已经落库，迁移版本却没有更新。

解决：

迁移脚本需要具备可恢复能力：先检查列和索引是否存在，再决定是否创建。当前项目已修复：

```text
c5c51f8 fix: make model default migration resumable
```

### 15.4 MySQL 不允许同表子查询更新

现象：

迁移中执行：

```sql
UPDATE model_nodes
SET is_default = 1
WHERE id = (
  SELECT id FROM model_nodes ...
)
```

MySQL 报错 1093。

解决：

拆成两步：

1. 先查询默认模型 ID。
2. 再按 ID 更新。

当前项目已修复：

```text
21f14ea fix: make default model migration mysql-compatible
```

### 15.5 测试不要直接读取生产 .env

现象：

远端跑 pytest 时受到生产 `.env` 影响，例如 mock 开关、管理员密码、数据库配置不符合测试预期。

解决：

测试时临时移开 `.env`：

```bash
mv .env .env.runtime
cd backend
/opt/c-check/.venv/bin/python -m pytest tests -q
cd /opt/c-check
mv .env.runtime .env
```

### 15.6 Python 3.14 与当前 SQLAlchemy 组合不适合本地测试

现象：

Windows 本机 Python 3.14 运行后端测试，SQLAlchemy 类型解析报错。

解决：

使用项目更稳定的 Python 3.12 环境跑测试。本次远端 Python 3.12 测试通过：

```text
79 passed
```

### 15.7 前端构建日志的特殊字符可能打断本地输出

现象：

远程构建 Vite 时输出 `✓` 等字符，本地 PowerShell/GBK 环境可能出现编码异常。

解决：

本地执行远程脚本时设置：

```powershell
$env:PYTHONIOENCODING='utf-8'
```

或减少对特殊字符日志的解析。

### 15.8 systemd 不建议直接解析复杂 .env

问题：

`CORS_ORIGINS='["..."]'` 这类 JSON 字符串在 systemd `EnvironmentFile` 中容易出现引号解析问题。

解决：

不在 systemd service 中设置 `EnvironmentFile`，让 FastAPI 配置模块自动读取 `/opt/c-check/.env`。

### 15.9 先用 mock 模型跑通系统，再接真实 VLLM

原因：

30B+ 模型下载和 VLLM 启动耗时较长，还受 Hugging Face、ModelScope、磁盘和显存影响。部署主站时不应被模型下载阻塞。

成功经验：

1. 先设置 `MOCK_MODEL_ENABLED=true`。
2. 注册 `mock://local` 模型。
3. 验证登录、提交、队列、报告、下载全链路。
4. 后续再接入真实 OpenAI-compatible VLLM endpoint。

## 16. 常用运维命令

查看服务：

```bash
systemctl status c-check-api c-check-worker nginx mysql redis-server --no-pager
```

查看日志：

```bash
journalctl -u c-check-api -n 160 --no-pager
journalctl -u c-check-worker -n 160 --no-pager
tail -n 100 /var/log/nginx/access.log
tail -n 100 /var/log/nginx/error.log
```

重启服务：

```bash
systemctl restart c-check-api c-check-worker
nginx -t && systemctl reload nginx
```

更新代码：

```bash
cd /opt/c-check
git fetch origin master
git reset --hard origin/master
cd backend
/opt/c-check/.venv/bin/alembic upgrade head
systemctl restart c-check-api c-check-worker
cd /opt/c-check/frontend
export PATH=/opt/node22/bin:$PATH
npm ci
npm run build
nginx -t && systemctl reload nginx
```

确认版本：

```bash
cd /opt/c-check
git rev-parse --short HEAD
cd backend
/opt/c-check/.venv/bin/alembic current
```

确认端口：

```bash
ss -ltnp | grep -E ':(8800|8000|3306|6379) '
```

## 17. 后续接入真实 VLLM 模型建议

主站已经可用后，再单独部署模型服务。

### 17.1 2026-06-06 结构化输出审核部署经验

本次线上问题是：前端提交 C 文件审查后，模型返回内容被截断或不符合后端结构要求，前端显示 `model returned an invalid structured response`。后续排查发现，需要同时处理三类风险：

1. 模型可能返回非完整 JSON、Markdown 包裹 JSON、额外解释文本或被截断的 JSON。
2. 旧解析逻辑可能误抓内部 `finding` 子对象，导致错误信息不直观。
3. VLLM 的 `max_tokens` 不能等于模型上下文长度，否则会因为没有给输入提示词和源码预留空间而返回 400。

最终采用的方案：

1. 请求模型时启用 `response_format=json_schema`，让 VLLM 在生成阶段约束输出结构。
2. 后端继续使用 Pydantic schema 做二次审核，只有完全符合 `summary / score / findings` 结构的结果才生成报告。
3. 审核失败时，将校验错误、原始响应片段和修正要求反馈给模型，自动重试。
4. 默认最多重试 3 次，仍失败则任务置为 failed，前端可在模型日志中查看每次失败原因。
5. 将 `findings` 上限控制为 8 条，避免输出过长导致 JSON 截断。

关键配置：

```dotenv
MODEL_MAX_ATTEMPTS=3
MODEL_MAX_TOKENS=2048
MODEL_STRUCTURED_OUTPUTS_ENABLED=true
```

配置经验：

- 4K 上下文模型建议保持 `MODEL_MAX_TOKENS=2048`，不要设置为 4096。
- 如果模型服务不支持 `response_format=json_schema`，可临时设置 `MODEL_STRUCTURED_OUTPUTS_ENABLED=false` 回退到普通 JSON object，但仍会保留后端二次审核。
- 生产环境推荐保持 `MODEL_STRUCTURED_OUTPUTS_ENABLED=true`。

本次部署命令：

```bash
cd /opt/c-check
git fetch origin master
git checkout master
git reset --hard origin/master

grep -q '^MODEL_STRUCTURED_OUTPUTS_ENABLED=' /etc/c-check/c-check.env \
  && sed -i 's/^MODEL_STRUCTURED_OUTPUTS_ENABLED=.*/MODEL_STRUCTURED_OUTPUTS_ENABLED=true/' /etc/c-check/c-check.env \
  || printf '\nMODEL_STRUCTURED_OUTPUTS_ENABLED=true\n' >> /etc/c-check/c-check.env

DEPLOY_ENV=/etc/c-check/c-check.env bash /opt/c-check/deploy/native/c-check-deploy.sh update
```

部署后检查：

```bash
cd /opt/c-check
git rev-parse --short HEAD
grep -E '^(MODEL_MAX_ATTEMPTS|MODEL_MAX_TOKENS|MODEL_STRUCTURED_OUTPUTS_ENABLED)=' /etc/c-check/c-check.env
systemctl is-active c-check-api c-check-worker c-check-vllm nginx mysql redis-server
ss -ltnp | grep -E ':(8000|8001|8800) '
```

本次成功状态：

- 代码版本：`77c16db feat: add structured model output audit`
- 线上入口：`http://180.127.11.166:15188/`
- 线上入口返回：`HTTP/1.1 200 OK`
- 服务状态：`c-check-api`、`c-check-worker`、`c-check-vllm`、`nginx`、`mysql`、`redis-server` 均为 active。
- 真实 C 代码审查验证通过，任务第一次尝试完成，生成报告和 findings。

模型日志排查经验：

- 失败任务会在 `review_tasks.model_log` 中记录 `Attempt N started/failed/succeeded`。
- 如果模型返回了非法 JSON，日志会保存 `Raw model response`。
- 如果 VLLM 返回 HTTP 400，日志会保存响应正文，例如 `max_tokens is too large`。
- 前端工作台和历史记录中都可以点击“模型日志”查看这些内容。

这次经验里最重要的一点：不要只依赖提示词让模型“自觉输出 JSON”。更稳的工程方案是“生成阶段 JSON Schema 约束 + 服务端 schema 二次审核 + 带错误反馈的有限重试 + 前端可见日志”。

建议顺序：

1. 准备模型目录和缓存目录，确认磁盘空间。
2. 优先尝试 Hugging Face 下载；如果网络不可用，使用 ModelScope。
3. 用 VLLM 启动 OpenAI-compatible API。
4. 先验证：

```bash
curl -i http://127.0.0.1:<vllm_port>/v1/models
```

5. 在 C-Check 管理后台新增模型节点：

```json
{
  "display_name": "Qwen Coder 30B",
  "model_identifier": "<served_model_name>",
  "base_url": "http://127.0.0.1:<vllm_port>",
  "api_key": "<optional_api_key>",
  "timeout_seconds": 600,
  "is_enabled": true,
  "description": "VLLM OpenAI-compatible local GPU node"
}
```

6. 健康检查通过后，设为默认模型。
7. 将 `.env` 的 `MOCK_MODEL_ENABLED` 改为 `false`，重启 API 和 worker。

## 18. 成功标准清单

部署完成后至少满足：

- `systemctl is-active c-check-api c-check-worker nginx` 均为 `active`。
- `alembic current` 显示 `0003_model_default (head)`。
- `ss -ltnp` 显示 Nginx 监听 `0.0.0.0:8800`。
- `curl --noproxy "*" -i http://180.127.11.167:24164/` 返回 `HTTP/1.1 200 OK`。
- 浏览器能打开登录页。
- 管理员能登录。
- 模型列表至少有一个可用模型。
- 能提交 C 代码审查任务。
- worker 能完成任务。
- 能打开报告并下载 Markdown。

## 19. 2026-06-07 云服务器重启后的恢复经验

本次云服务器重启后，C-Check 恢复耗时较长，主要原因不是单个服务启动失败，而是 SSH、端口映射、Nginx 配置、本地隧道和 Windows 命令转义同时变化或互相影响。

### 19.1 本次新连接信息

云平台重启后 SSH 入口发生变化：

```text
移动 SSH：223.109.239.36:13912
电信 SSH：180.127.11.177:13912
公网 Web：223.109.239.36:13958 -> 服务器内网 8800
公网 Web：180.127.11.177:13958 -> 服务器内网 8800
```

注意：文档只记录连接形态，不记录真实 SSH 密码、API Key、数据库密码等敏感信息。

### 19.2 为什么这次启动花了较久

1. **SSH 端口变更**：原来的 SSH 端口不可再用，需要改为 `13912` 重新连接。
2. **Nginx 监听端口不匹配**：云平台预留端口映射到服务器内网 `8800`，但服务器上的 `/etc/c-check/c-check.env` 仍是 `WEB_PORT=80`，导致一键部署脚本会把 Nginx 写回 80。
3. **公网访问需要带外网端口**：本次可用入口是 `http://223.109.239.36:13958/` 或 `http://180.127.11.177:13958/`，不是裸 IP 的 80 端口。
4. **本地隧道端口被旧进程占用**：Windows 本地 `127.0.0.1:18000` 被旧 Python 进程占着，页面访问超时，需要结束旧进程后重新建立 SSH 隧道。
5. **Windows PowerShell 与远程 Bash 引号冲突**：包含 `$(date ...)`、JSON 字符串、`||` 的远程命令容易被本地 PowerShell 提前解析，曾导致 `CORS_ORIGINS` 写坏。
6. **VLLM active 不代表立刻完全可用**：`c-check-vllm` systemd 进入 active 后，还需要等待模型权重和 API server 完成初始化；未带 API Key 请求 `/v1/models` 返回 `401 Unauthorized` 反而说明服务已经响应。

### 19.3 推荐恢复顺序

先确认 SSH：

```bash
ssh -p 13912 root@223.109.239.36
```

检查项目和服务：

```bash
cd /opt/c-check
git log -1 --oneline
systemctl is-active c-check-api c-check-worker nginx mysql redis-server c-check-vllm
ss -ltnp | grep -E ':(8800|8000|8001) '
```

确认 `/etc/c-check/c-check.env` 的 Web 端口与云平台映射一致：

```bash
grep -E '^(WEB_PORT|PUBLIC_ORIGIN|CORS_ORIGINS)=' /etc/c-check/c-check.env
```

本次正确配置示例：

```dotenv
WEB_PORT=8800
PUBLIC_ORIGIN=http://223.109.239.36:13958
CORS_ORIGINS='["http://223.109.239.36:13958", "http://180.127.11.177:13958", "http://223.109.239.36", "http://180.127.11.177", "http://localhost", "http://127.0.0.1:18000"]'
```

如果 Nginx 没有监听 `8800`，补写配置并重载：

```bash
grep -q 'listen 0.0.0.0:8800;' /etc/nginx/sites-available/c-check.conf \
  || sed -i '/listen 0.0.0.0:80;/a\    listen 0.0.0.0:8800;' /etc/nginx/sites-available/c-check.conf

nginx -t
systemctl reload nginx
curl -I http://127.0.0.1:8800/
```

重启后端和 worker：

```bash
systemctl restart c-check-api c-check-worker
systemctl is-active c-check-api c-check-worker
```

确认 VLLM：

```bash
systemctl status c-check-vllm --no-pager -l
curl -i http://127.0.0.1:8001/v1/models
```

如果返回 `401 Unauthorized`，说明 VLLM API server 已经响应，只是需要带 API Key。

公网检查：

```bash
curl -I http://223.109.239.36:13958/
curl -I http://180.127.11.177:13958/
```

本地 Windows 浏览器隧道：

```powershell
Get-NetTCPConnection -LocalPort 18000 -ErrorAction SilentlyContinue
Stop-Process -Id <旧占用进程ID> -Force
Start-Process -FilePath ssh.exe -ArgumentList @(
  '-N',
  '-L','18000:127.0.0.1:8800',
  '-o','ExitOnForwardFailure=yes',
  '-o','StrictHostKeyChecking=no',
  '-p','13912',
  'root@223.109.239.36'
) -WindowStyle Hidden
curl.exe -I http://127.0.0.1:18000/admin
```

### 19.4 PowerShell 远程命令注意事项

从 Windows PowerShell 直接执行复杂 SSH 命令时，尽量避免：

- `$(date ...)` 这类会被本地 PowerShell 抢先解析的语法。
- 多层嵌套 JSON 引号。
- Bash 专属的 `|| true` 被本地解析。

更稳的做法：

1. 简单命令直接 SSH 执行。
2. 复杂配置修改用远程 `python3` 脚本生成文件内容。
3. 修改 `/etc/c-check/c-check.env` 后，必须用 `grep` 回读确认格式。
4. 对 `CORS_ORIGINS` 这类 JSON 数组，确认它仍是合法 JSON 字符串数组。

### 19.5 本次成功状态

- 代码版本：`afc5a81 fix: place auto refresh label before switch`
- 本地隧道：`http://127.0.0.1:18000/admin`
- 移动公网：`http://223.109.239.36:13958/`
- 电信公网：`http://180.127.11.177:13958/`
- 服务状态：`c-check-api`、`c-check-worker`、`c-check-vllm`、`nginx`、`mysql`、`redis-server` 均为 `active`
- Nginx：监听 `0.0.0.0:8800`
- 后端：监听 `127.0.0.1:8000`
- VLLM：监听 `127.0.0.1:8001`

## 20. 2026-06-08 双 GPU 服务器部署验证经验

本节记录双 GPU 云服务器的实操流程和卡点。不要把 SSH 密码、VLLM API Key、数据库密码写入 Git 文档；这里只记录命令模板和判断标准。

### 20.1 先确认是不是目标机器

拿到新 SSH 入口后，先做三类检查：公网端口、系统/GPU、已有部署状态。

Windows 本地：

```powershell
Test-NetConnection 223.109.239.11 -Port 20432
Test-NetConnection 180.127.11.169 -Port 20432
ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no -p 20432 root@223.109.239.11 "echo ok"
```

如果 `BatchMode=yes` 返回 `Permission denied (publickey,password)`，说明端口通但需要密码；如果是 `Connection refused` 或 `TcpTestSucceeded=False`，说明云平台 SSH 映射或实例网络不可达，继续部署没有意义。

登录服务器后：

```bash
hostname
whoami
grep PRETTY_NAME /etc/os-release
uname -r
nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader
docker --version || true
docker info --format '{{json .Runtimes}}' || true
git -C /opt/c-check rev-parse --short HEAD || true
test -f /etc/c-check/c-check.env && echo env-present || echo env-missing
systemctl is-active c-check-api c-check-worker nginx mysql redis-server docker 2>/dev/null || true
ss -ltnp | grep -E ':(8800|8000|8001|8101) ' || true
df -h /
```

本次确认到的可用硬件形态：

```text
GPU_COUNT=2
0, NVIDIA A100-SXM4-40GB, 40960 MiB
1, NVIDIA A100-SXM4-40GB, 40960 MiB
```

### 20.2 主站优先跑通，不要被模型下载阻塞

主站和模型服务解耦。双 GPU 服务器上也应先让 C-Check Web/API/worker/MySQL/Redis/Nginx 可用，再启动 VLLM。

一键入口仍优先使用：

```bash
PUBLIC_HOST_ALT=180.127.11.169 \
ADMIN_PASSWORD='<替换为管理员密码>' \
MOCK_MODEL_ENABLED=false \
MODEL_DEPLOYMENT_ENABLED=true \
VLLM_API_KEY='<替换为VLLM_API_KEY>' \
DEPLOY_ENV=/etc/c-check/c-check.env \
bash /opt/c-check/deploy/native/c-check-deploy.sh provision 223.109.239.11 18000 8800
```

如果 `provision/install` 中途因为 GitHub SSL timeout 中断，但 `/opt/c-check` 已经 clone 成功，可以直接从已克隆代码恢复，不必删除重来：

```bash
cd /opt/c-check
git rev-parse --short HEAD
grep -E '^(WEB_PORT|PUBLIC_ORIGIN|CORS_ORIGINS|MOCK_MODEL_ENABLED|MODEL_DEPLOYMENT_ENABLED)=' /etc/c-check/c-check.env

DEPLOY_ENV=/etc/c-check/c-check.env bash /opt/c-check/deploy/native/c-check-deploy.sh recover
```

如果 `recover` 还不够，因为前一次中断在依赖安装前，可以按以下顺序补齐：

```bash
cd /opt/c-check
.venv/bin/python --version || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e "backend[test]"
cd /opt/c-check/backend
/opt/c-check/.venv/bin/alembic upgrade head

cd /opt/c-check/frontend
npm ci --no-audit --no-fund
npm run build

DEPLOY_ENV=/etc/c-check/c-check.env bash /opt/c-check/deploy/native/c-check-deploy.sh recover
```

主站成功标准：

```bash
systemctl is-active c-check-api c-check-worker nginx mysql redis-server
ss -ltnp | grep -E ':(8800|8000) '
curl -I http://127.0.0.1:8800/
curl -sS http://127.0.0.1:8800/api/models
```

预期：

- `c-check-api`、`c-check-worker`、`nginx`、`mysql`、`redis-server` 均为 `active`
- Nginx 监听 `0.0.0.0:8800`
- API 监听 `127.0.0.1:8000`
- 未登录请求 `/api/models` 返回 `{"detail":"Not authenticated"}`，说明 API 代理链路正常

### 20.3 Docker + NVIDIA runtime 检查

双 GPU VLLM Docker 路线要求 Docker 和 NVIDIA Container Toolkit 都可用。

安装 Docker：

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release git openssl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
```

安装 NVIDIA Container Toolkit：

```bash
rm -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg /etc/apt/sources.list.d/nvidia-container-toolkit.list
curl -4 -fsSL --connect-timeout 20 --max-time 120 --retry 3 https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -4 -fsSL --connect-timeout 20 --max-time 120 --retry 3 https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
```

检查：

```bash
nvidia-container-cli info | head -80
docker info --format '{{json .Runtimes}}' | grep nvidia
```

如果能看到两张 GPU，且 Docker runtimes 里有 `nvidia`，GPU 容器运行时已就绪。

本次踩坑：Docker Hub 不稳定，以下命令可能失败：

```bash
docker manifest inspect hello-world:latest
docker manifest inspect vllm/vllm-openai:latest
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

典型错误：

```text
TLS handshake timeout
EOF
failed to resolve reference "docker.io/nvidia/cuda..."
```

结论：这类错误是 Docker Hub 网络问题，不代表 NVIDIA runtime 不可用。若 `nvidia-container-cli info` 正常，可转为本机 Python vLLM 路线，或者配置稳定的 Docker 镜像源后再跑容器路线。

### 20.4 Docker 路线启动双 GPU VLLM

Docker Hub 可用时，使用项目脚本启动，关键变量是 `TENSOR_PARALLEL_SIZE=2`。

```bash
export VLLM_IMAGE=vllm/vllm-openai:latest
export VLLM_API_KEY='<替换为VLLM_API_KEY>'
export TENSOR_PARALLEL_SIZE=2
export GPU_MEMORY_UTILIZATION=0.90
export MAX_MODEL_LEN=8192
export MODEL_CACHE_DIR=/data/huggingface

bash /opt/c-check/deploy/models/deploy-vllm-model.sh \
  --source modelscope \
  --repository deepseek-ai/deepseek-coder-14b-instruct \
  --served-model-name deepseek-coder-14b-instruct \
  --base-url http://127.0.0.1:8101 \
  --port 8101 \
  --service-name c-check-vllm-deepseek-coder-14b
```

检查：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker logs -f c-check-vllm-deepseek-coder-14b
curl -i -H "Authorization: Bearer <VLLM_API_KEY>" http://127.0.0.1:8101/v1/models
nvidia-smi
```

成功标准：

- `/v1/models` 返回的 `id` 等于 `deepseek-coder-14b-instruct`
- `nvidia-smi` 看到两个 GPU 都有显存占用
- VLLM 日志中没有 NCCL、CUDA OOM、模型路径错误

### 20.5 本机 Python vLLM 路线

当 Docker Hub 长时间不可用时，用独立 `/opt/vllm` Python 环境绕过 Docker 镜像下载。

安装：

```bash
python3 -m venv /opt/vllm
/opt/vllm/bin/python -m pip install --upgrade pip setuptools wheel
/opt/vllm/bin/python -m pip install 'vllm==0.22.1'

/opt/vllm/bin/python - <<'PY'
import torch, vllm
print("torch", torch.__version__, "cuda", torch.cuda.is_available(), "devices", torch.cuda.device_count())
print("vllm", vllm.__version__)
PY
```

注意：`vllm==0.22.1` 会下载 PyTorch、CUDA、NCCL、Triton、FlashInfer 等大 wheel，体积数 GB。SSH 可能因云平台网络抖动断开，不要立刻判定失败；重连后看：

```bash
pgrep -a -f 'install-vllm-native|pip install|vllm' || true
tail -n 80 /tmp/vllm-install.log
/opt/vllm/bin/python - <<'PY'
import torch, vllm
print(torch.__version__, torch.cuda.is_available(), torch.cuda.device_count(), vllm.__version__)
PY
```

本机 vLLM systemd 示例：

```bash
cat >/etc/systemd/system/c-check-vllm.service <<'EOF'
[Unit]
Description=C-Check VLLM OpenAI API
After=network.target

[Service]
Type=simple
Environment=HF_TOKEN=
Environment=VLLM_USE_MODELSCOPE=true
Environment=CUDA_VISIBLE_DEVICES=0,1
WorkingDirectory=/opt/c-check
ExecStart=/opt/vllm/bin/python -m vllm.entrypoints.openai.api_server \
  --host 127.0.0.1 \
  --port 8001 \
  --model deepseek-ai/deepseek-coder-14b-instruct \
  --served-model-name deepseek-coder-14b-instruct \
  --api-key <替换为VLLM_API_KEY> \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.90 \
  --max-model-len 8192
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now c-check-vllm
journalctl -u c-check-vllm -f
```

验证：

```bash
systemctl is-active c-check-vllm
curl -i http://127.0.0.1:8001/v1/models
curl -i -H "Authorization: Bearer <VLLM_API_KEY>" http://127.0.0.1:8001/v1/models
nvidia-smi
```

未带 API Key 返回 `401 Unauthorized` 说明 API server 已响应；带 API Key 能返回模型列表才算可注册。

### 20.6 注册双 GPU 模型节点到 C-Check

VLLM 可用后，把节点登记为默认模型：

```bash
grep -q '^REGISTER_VLLM_MODEL=' /etc/c-check/c-check.env \
  && sed -i 's/^REGISTER_VLLM_MODEL=.*/REGISTER_VLLM_MODEL=true/' /etc/c-check/c-check.env \
  || echo 'REGISTER_VLLM_MODEL=true' >> /etc/c-check/c-check.env

grep -q '^VLLM_DISPLAY_NAME=' /etc/c-check/c-check.env \
  && sed -i 's/^VLLM_DISPLAY_NAME=.*/VLLM_DISPLAY_NAME=DeepSeek-Coder 14B Instruct TP2/' /etc/c-check/c-check.env \
  || echo 'VLLM_DISPLAY_NAME=DeepSeek-Coder 14B Instruct TP2' >> /etc/c-check/c-check.env

grep -q '^VLLM_MODEL_IDENTIFIER=' /etc/c-check/c-check.env \
  && sed -i 's/^VLLM_MODEL_IDENTIFIER=.*/VLLM_MODEL_IDENTIFIER=deepseek-coder-14b-instruct/' /etc/c-check/c-check.env \
  || echo 'VLLM_MODEL_IDENTIFIER=deepseek-coder-14b-instruct' >> /etc/c-check/c-check.env

grep -q '^VLLM_BASE_URL=' /etc/c-check/c-check.env \
  && sed -i 's#^VLLM_BASE_URL=.*#VLLM_BASE_URL=http://127.0.0.1:8001#' /etc/c-check/c-check.env \
  || echo 'VLLM_BASE_URL=http://127.0.0.1:8001' >> /etc/c-check/c-check.env

DEPLOY_ENV=/etc/c-check/c-check.env bash /opt/c-check/deploy/native/c-check-deploy.sh update
systemctl restart c-check-api c-check-worker
```

检查：

```bash
curl -sS http://127.0.0.1:8800/api/models
journalctl -u c-check-worker -n 100 --no-pager
```

如果未登录 `/api/models` 返回 401，用浏览器登录后在管理后台确认默认模型显示为 TP2 节点。

### 20.7 SSH/NAT 抖动时的判断

本次多次出现 SSH 入口短暂恢复后又不可达：

```powershell
Test-NetConnection 223.109.239.11 -Port 20432
Test-NetConnection 180.127.11.169 -Port 20432
```

判断规则：

- `Permission denied (publickey,password)`：SSH 服务和端口映射正常，只是需要密码。
- `Connection refused`：远端端口没有监听或云平台映射断开。
- `TcpTestSucceeded=False`：本地无法到达该公网端口。
- Web 返回 `502 Bad Gateway`：云网关通了，但没有正常转发到服务器内 Nginx，或内网目标端口不对。
- 服务器内 `curl -I http://127.0.0.1:8800/` 是 200，但公网 502：优先查云平台 Web 端口映射，不要先改应用代码。

SSH 不稳定时，所有长任务都写日志文件，不要依赖交互输出：

```bash
DEPLOY_ENV=/etc/c-check/c-check.env bash /opt/c-check/deploy/native/c-check-deploy.sh install \
  >/tmp/c-check-install.log 2>&1 &

tail -f /tmp/c-check-install.log
```

vLLM 安装同理：

```bash
bash /tmp/install-vllm-native.sh >/tmp/vllm-install.log 2>&1 &
tail -f /tmp/vllm-install.log
```

### 20.8 本次阶段性结论

已验证成功：

- 双 GPU 硬件识别正常。
- NVIDIA Container Toolkit 安装后，`nvidia-container-cli info` 能看到两张 A100。
- C-Check 主站在服务器内已跑通，`127.0.0.1:8800` 返回 200。
- API、worker、Nginx、MySQL、Redis 均可启动为 active。

未完成项：

- Docker Hub 拉取 `hello-world`、`nvidia/cuda`、`vllm/vllm-openai` 不稳定，容器路线未完成。
- 已开始本机 Python vLLM 安装，但 SSH/NAT 中断后未能最终确认 `/opt/vllm` 安装完成。
- 公网 Web 返回云网关 502，需要云平台确认公网端口映射到服务器内网 `8800`。
- SSH `20432` 多次不可达，需要云平台恢复 SSH/NAT 后继续。

恢复后第一组命令：

```bash
systemctl is-active c-check-api c-check-worker nginx mysql redis-server docker
curl -I http://127.0.0.1:8800/
pgrep -a -f 'pip install|vllm' || true
tail -n 100 /tmp/vllm-install.log 2>/dev/null || true
/opt/vllm/bin/python - <<'PY'
import torch, vllm
print(torch.__version__, torch.cuda.is_available(), torch.cuda.device_count(), vllm.__version__)
PY
```

如果 vLLM 安装已完成，直接写 `c-check-vllm.service` 并使用 `--tensor-parallel-size 2` 启动。
