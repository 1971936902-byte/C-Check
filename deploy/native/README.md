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
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh install
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh update
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh status
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh logs c-check-worker
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh backup
```

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
