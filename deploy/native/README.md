# C-Check Native One-Command Deployment

This directory contains the native Linux deployment path used for a single C-Check host:

- FastAPI runs from a Python virtual environment.
- Celery worker runs as a systemd service.
- MySQL 8 and Redis run as host services.
- Vue frontend is built into `frontend/dist`.
- Nginx serves the frontend and proxies `/api/`.
- Optional VLLM nodes stay decoupled and can be registered by configuration.

## Supported Server

Recommended baseline:

- Ubuntu 22.04 or 24.04
- Root or sudo access
- 4 CPU cores, 8 GB RAM for web/backend
- GPU server only when colocating VLLM
- Open inbound web port configured by `WEB_PORT`

The backend requires Python 3.12 or newer. On Ubuntu 22.04, the installer automatically uses `uv` to install Python 3.12 for the project virtual environment when the system Python is older.

## Quick Start

For a new server, prefer the one-command provision path. It generates `/etc/c-check/c-check.env`, installs dependencies, builds the frontend, writes systemd/Nginx config, starts services, and runs health checks:

```bash
git clone https://github.com/1971936902-byte/C-Check.git /opt/c-check
cd /opt/c-check

sudo DEPLOY_ENV=/etc/c-check/c-check.env \
  PUBLIC_HOST_ALT=180.127.11.177 \
  bash deploy/native/c-check-deploy.sh provision 223.109.239.36 13958 8800
```

Arguments are:

- `223.109.239.36`: primary public host
- `13958`: public web port shown by the cloud provider
- `8800`: internal server port mapped by the cloud provider

After a server reboot or SSH/port mapping change, use:

```bash
cd /opt/c-check
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh recover
```

The older manual env editing path is still available when you want full control:

```bash
git clone https://github.com/1971936902-byte/C-Check.git /opt/c-check
cd /opt/c-check

mkdir -p /etc/c-check
cp deploy/native/c-check.env.example /etc/c-check/c-check.env
vim /etc/c-check/c-check.env

sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh install
```

After installation, open the configured `PUBLIC_ORIGIN`.

## Required Edits

Before running `install`, replace all `CHANGE_ME` values in `/etc/c-check/c-check.env`.
For a Chinese field-by-field guide, read [配置说明文档.md](配置说明文档.md).

Important fields:

- `PUBLIC_ORIGIN`: browser URL, for example `http://180.127.11.167:24164`
- `CORS_ORIGINS`: JSON list containing the browser URL
- `MYSQL_PASSWORD`: strong database password
- `JWT_SECRET`: at least 32 random characters
- `ADMIN_PASSWORD`: at least 12 characters
- `WEB_PORT`: internal Nginx listening port, for example `8800`

## Commands

```bash
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh provision 223.109.239.36 13958 8800
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh install
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh update
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh recover
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh health
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh status
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh logs c-check-worker
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh backup
```

## Current Zhixing Cloud Notes

The current test deployment uses cloud reserved port mapping instead of public
port 80:

```text
SSH: ssh root@223.109.239.36 -p 13920
SSH: ssh root@180.127.11.177 -p 13920

Web: http://223.109.239.36:13970/
Web: http://180.127.11.177:13970/

Mapping:
external 13920 -> internal 22
external 13970 -> internal 8800
```

Keep Nginx listening on the internal `WEB_PORT=8800`, and set the browser
origin to the external web port:

```dotenv
WEB_PORT=8800
PUBLIC_ORIGIN=http://223.109.239.36:13970
CORS_ORIGINS='["http://223.109.239.36:13970","http://180.127.11.177:13970","http://223.109.239.36","http://180.127.11.177","http://localhost","http://127.0.0.1:18000"]'
```

Important: keep the single quotes around `CORS_ORIGINS`. The deploy script
sources `/etc/c-check/c-check.env`; without those quotes, the JSON list can be
misparsed before Pydantic reads it.

Current local-test admin account:

```text
username: admin
password: admin
```

For config validation, keep `ADMIN_PASSWORD` in `/etc/c-check/c-check.env` as a
strong password. If the database admin account needs to be reset to the local
test password, run:

```bash
cd /opt/c-check
set -a
source /etc/c-check/c-check.env
set +a

/opt/c-check/.venv/bin/python - <<'PY'
from sqlalchemy import select
from app.core.security import hash_password
from app.db.models import User, new_uuid
from app.db.session import SessionLocal

username = "admin"
password = "admin"

with SessionLocal() as db:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        user = User(id=new_uuid(), username=username)
        db.add(user)
    user.password_hash = hash_password(password)
    user.role = "admin"
    user.is_enabled = True
    user.token_version = (user.token_version or 0) + 1
    db.commit()
PY
```

Verify after deploy or reboot:

```bash
cd /opt/c-check
DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh status
curl -I http://127.0.0.1:8800/
curl --noproxy "*" -I http://223.109.239.36:13970/
curl --noproxy "*" -i http://223.109.239.36:13970/api/models
```

The unauthenticated `/api/models` check should return `401 Unauthorized`; that
still confirms Nginx is proxying `/api/` to FastAPI.

## Register A Local VLLM Node

If VLLM is already listening on the same server, set:

```dotenv
REGISTER_VLLM_MODEL=true
VLLM_DISPLAY_NAME=Qwen2.5 Coder 32B AWQ
VLLM_MODEL_IDENTIFIER=qwen2.5-coder-32b-awq
VLLM_BASE_URL=http://127.0.0.1:8001
VLLM_API_KEY=<your-vllm-api-key>
VLLM_TIMEOUT_SECONDS=600
```

Then run:

```bash
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh update
```

## What The Installer Writes

- `${APP_DIR}/.env`
- `${APP_DIR}/.venv`
- `/etc/systemd/system/c-check-api.service`
- `/etc/systemd/system/c-check-worker.service`
- `/etc/nginx/sites-available/c-check.conf`
- `/etc/nginx/sites-enabled/c-check.conf`
- MySQL database and users from the env file

## Notes

- The installer is idempotent for normal updates.
- Existing source checkout is reset to `origin/${BRANCH}`.
- The script refuses placeholder secrets.
- Backups are stored under `${APP_DIR}/backups/<timestamp>`.
- Node.js is installed from NodeSource first. If that network path fails, the installer falls back to a configurable Node.js tarball mirror through `NODE_VERSION`, `NODE_DIST_URL`, and `NODE_INSTALL_DIR`.
- Use the existing Docker Compose `start.sh` when you prefer containerized deployment.
