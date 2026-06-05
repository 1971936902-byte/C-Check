# C-Check

C-Check is a C language AI code review platform built with Vue 3, FastAPI, Celery, Redis, MySQL, and OpenAI-compatible model nodes such as VLLM.

## Deployment Options

### Native Linux one-command deployment

Use this path for cloud servers like the current production deployment:

```bash
git clone https://github.com/1971936902-byte/C-Check.git /opt/c-check
cd /opt/c-check
mkdir -p /etc/c-check
cp deploy/native/c-check.env.example /etc/c-check/c-check.env
vim /etc/c-check/c-check.env
sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh install
```

See [deploy/native/README.md](deploy/native/README.md) for configuration, updates, backups, and VLLM registration.

### Docker Compose deployment

```bash
cp .env.example .env
vim .env
./start.sh start
```

See [docs/deployment-linux.md](docs/deployment-linux.md) for the containerized path.

## Local Development

Backend:

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
pytest
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

## Current Production Notes

The latest native deployment experience and VLLM model notes are documented in:

- [docs/cloud-native-deployment-runbook-2026-06-04.md](docs/cloud-native-deployment-runbook-2026-06-04.md)
- [docs/vllm-model-deployment-changelog-2026-06-04.md](docs/vllm-model-deployment-changelog-2026-06-04.md)
