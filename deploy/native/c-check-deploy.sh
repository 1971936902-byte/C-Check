#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_ENV="${DEPLOY_ENV:-/etc/c-check/c-check.env}"

log() {
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run as root, for example: sudo DEPLOY_ENV=${DEPLOY_ENV} bash $0 $*"
}

load_env() {
  [[ -f "${DEPLOY_ENV}" ]] || die "Missing ${DEPLOY_ENV}. Copy deploy/native/c-check.env.example and edit it first."
  set -a
  # shellcheck disable=SC1090
  source "${DEPLOY_ENV}"
  set +a

  APP_DIR="${APP_DIR:-/opt/c-check}"
  APP_USER="${APP_USER:-c-check}"
  BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
  BACKEND_PORT="${BACKEND_PORT:-8000}"
  WEB_PORT="${WEB_PORT:-8800}"
  PYTHON_BIN="${PYTHON_BIN:-python3}"
  PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
  NODE_MAJOR="${NODE_MAJOR:-22}"
  NODE_VERSION="${NODE_VERSION:-22.21.1}"
  NODE_DIST_URL="${NODE_DIST_URL:-https://npmmirror.com/mirrors/node}"
  NODE_INSTALL_DIR="${NODE_INSTALL_DIR:-/opt/nodejs/${NODE_VERSION}}"
  STORAGE_PATH="${STORAGE_PATH:-${APP_DIR}/uploads}"
  MODEL_MAX_ATTEMPTS="${MODEL_MAX_ATTEMPTS:-3}"
}

validate_env() {
  local required=(
    REPO_URL BRANCH APP_DIR WEB_PORT BACKEND_HOST BACKEND_PORT PUBLIC_ORIGIN CORS_ORIGINS
    MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD MYSQL_ROOT_LOGIN REDIS_URL
    JWT_SECRET ADMIN_USERNAME ADMIN_PASSWORD STORAGE_PATH
  )
  for name in "${required[@]}"; do
    [[ -n "${!name:-}" ]] || die "Set ${name} in ${DEPLOY_ENV}."
    [[ "${!name}" != *CHANGE_ME* ]] || die "Replace placeholder value for ${name} in ${DEPLOY_ENV}."
  done
}

install_os_packages() {
  log "Installing OS packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y \
    ca-certificates curl git nginx redis-server mysql-server \
    build-essential pkg-config xz-utils "${PYTHON_BIN}" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev"
}

python_version_ok() {
  "${PYTHON_BIN}" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv $(uv --version | awk '{print $2}') already installed"
    return
  fi
  log "Installing uv for Python ${PYTHON_VERSION} runtime management"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
}

create_backend_venv() {
  cd "${APP_DIR}"
  rm -rf .venv
  if python_version_ok; then
    "${PYTHON_BIN}" -m venv .venv
  else
    install_uv
    uv python install "${PYTHON_VERSION}"
    uv venv --seed --python "${PYTHON_VERSION}" .venv
  fi
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m ensurepip --upgrade
  fi
}

install_nodejs() {
  if command -v node >/dev/null 2>&1 && node -v | grep -q "^v${NODE_MAJOR}\\."; then
    log "Node.js $(node -v) already installed"
    return
  fi

  log "Installing Node.js ${NODE_MAJOR} from NodeSource"
  if curl -fsSL --connect-timeout 20 --retry 3 "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y nodejs \
    && command -v node >/dev/null 2>&1 \
    && node -v | grep -q "^v${NODE_MAJOR}\\."; then
    log "Node.js $(node -v) installed from NodeSource"
    return
  fi

  log "NodeSource install failed, falling back to Node.js ${NODE_VERSION} tarball"
  install_nodejs_tarball
}

install_nodejs_tarball() {
  local machine arch tarball url tmp_dir extracted_dir
  machine="$(uname -m)"
  case "${machine}" in
    x86_64|amd64) arch="x64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) die "Unsupported CPU architecture for Node.js tarball: ${machine}" ;;
  esac

  tarball="node-v${NODE_VERSION}-linux-${arch}.tar.xz"
  url="${NODE_DIST_URL%/}/v${NODE_VERSION}/${tarball}"
  tmp_dir="$(mktemp -d)"

  log "Downloading ${url}"
  curl -fL --connect-timeout 30 --retry 5 --retry-delay 3 -o "${tmp_dir}/${tarball}" "${url}"
  tar -xJf "${tmp_dir}/${tarball}" -C "${tmp_dir}"
  extracted_dir="${tmp_dir}/node-v${NODE_VERSION}-linux-${arch}"

  rm -rf "${NODE_INSTALL_DIR}"
  mkdir -p "$(dirname "${NODE_INSTALL_DIR}")"
  mv "${extracted_dir}" "${NODE_INSTALL_DIR}"
  ln -sfn "${NODE_INSTALL_DIR}/bin/node" /usr/local/bin/node
  ln -sfn "${NODE_INSTALL_DIR}/bin/npm" /usr/local/bin/npm
  ln -sfn "${NODE_INSTALL_DIR}/bin/npx" /usr/local/bin/npx
  rm -rf "${tmp_dir}"
  hash -r

  command -v node >/dev/null 2>&1 || die "Node.js tarball install did not create node command."
  node -v | grep -q "^v${NODE_MAJOR}\\." || die "Installed Node.js $(node -v) does not match NODE_MAJOR=${NODE_MAJOR}."
  log "Node.js $(node -v) installed from tarball"
}

sync_source() {
  log "Syncing source code"
  if [[ -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" fetch origin "${BRANCH}"
    git -C "${APP_DIR}" reset --hard "origin/${BRANCH}"
  else
    mkdir -p "$(dirname "${APP_DIR}")"
    git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
  fi
}

ensure_user_and_dirs() {
  log "Preparing user and directories"
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
  mkdir -p "${STORAGE_PATH}"
  chown -R "${APP_USER}:${APP_USER}" "${STORAGE_PATH}"
}

write_app_env() {
  log "Writing ${APP_DIR}/.env"
  cat >"${APP_DIR}/.env" <<EOF
DATABASE_URL=mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@127.0.0.1:3306/${MYSQL_DATABASE}
REDIS_URL=${REDIS_URL}
JWT_SECRET=${JWT_SECRET}
JWT_EXPIRE_MINUTES=${JWT_EXPIRE_MINUTES:-480}
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
UPLOAD_MAX_FILE_BYTES=${UPLOAD_MAX_FILE_BYTES:-1048576}
UPLOAD_MAX_ARCHIVE_BYTES=${UPLOAD_MAX_ARCHIVE_BYTES:-10485760}
UPLOAD_MAX_EXTRACTED_BYTES=${UPLOAD_MAX_EXTRACTED_BYTES:-10485760}
UPLOAD_MAX_FILES=${UPLOAD_MAX_FILES:-200}
UPLOAD_MAX_ARCHIVE_ENTRIES=${UPLOAD_MAX_ARCHIVE_ENTRIES:-1000}
UPLOAD_MAX_PATH_LENGTH=${UPLOAD_MAX_PATH_LENGTH:-512}
CORS_ORIGINS=${CORS_ORIGINS}
STORAGE_PATH=${STORAGE_PATH}
MOCK_MODEL_ENABLED=${MOCK_MODEL_ENABLED:-false}
MODEL_MAX_ATTEMPTS=${MODEL_MAX_ATTEMPTS}
ALLOW_INSECURE_DEFAULTS=${ALLOW_INSECURE_DEFAULTS:-false}
EOF
  chown root:"${APP_USER}" "${APP_DIR}/.env"
  chmod 0640 "${APP_DIR}/.env"
}

setup_database() {
  log "Configuring MySQL database"
  systemctl enable --now mysql
  ${MYSQL_ROOT_LOGIN} <<SQL
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'127.0.0.1' IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER '${MYSQL_USER}'@'127.0.0.1' IDENTIFIED BY '${MYSQL_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
  systemctl enable --now redis-server
}

install_backend() {
  log "Installing backend Python environment"
  create_backend_venv
  .venv/bin/python -m pip install --upgrade pip setuptools wheel
  .venv/bin/python -m pip install -e "backend[test]"
  cd backend
  "${APP_DIR}/.venv/bin/alembic" upgrade head
}

build_frontend() {
  log "Building frontend"
  cd "${APP_DIR}/frontend"
  if [[ -x "${NODE_INSTALL_DIR}/bin/node" ]]; then
    export PATH="${NODE_INSTALL_DIR}/bin:${PATH}"
  fi
  npm ci
  npm run build
}

write_systemd_units() {
  log "Writing systemd units"
  cat >/etc/systemd/system/c-check-api.service <<EOF
[Unit]
Description=C-Check FastAPI service
After=network.target mysql.service redis-server.service
Wants=mysql.service redis-server.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}/backend
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host ${BACKEND_HOST} --port ${BACKEND_PORT}
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

  cat >/etc/systemd/system/c-check-worker.service <<EOF
[Unit]
Description=C-Check Celery worker
After=network.target redis-server.service mysql.service
Wants=redis-server.service mysql.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}/backend
ExecStart=${APP_DIR}/.venv/bin/celery -A app.worker.celery_app worker --loglevel=INFO
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable c-check-api c-check-worker
}

write_nginx_config() {
  log "Writing Nginx config"
  cat >/etc/nginx/sites-available/c-check.conf <<EOF
server {
    listen 0.0.0.0:${WEB_PORT};
    server_name _;
    root ${APP_DIR}/frontend/dist;
    index index.html;

    client_max_body_size 64m;

    location /api/ {
        proxy_pass http://${BACKEND_HOST}:${BACKEND_PORT}/api/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 900s;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF
  ln -sf /etc/nginx/sites-available/c-check.conf /etc/nginx/sites-enabled/c-check.conf
  nginx -t
}

register_vllm_model() {
  [[ "${REGISTER_VLLM_MODEL:-false}" == "true" ]] || return 0
  log "Registering configured VLLM model node"
  cd "${APP_DIR}/backend"
  "${APP_DIR}/.venv/bin/python" - <<'PY'
import os
from sqlalchemy import select, update
from app.db.models import ModelNode, new_uuid
from app.db.session import SessionLocal

identifier = os.environ["VLLM_MODEL_IDENTIFIER"]
with SessionLocal() as db:
    db.execute(update(ModelNode).values(is_default=False))
    node = db.scalar(select(ModelNode).where(ModelNode.model_identifier == identifier))
    if node is None:
        node = ModelNode(id=new_uuid(), model_identifier=identifier)
        db.add(node)
    node.display_name = os.environ["VLLM_DISPLAY_NAME"]
    node.base_url = os.environ["VLLM_BASE_URL"]
    node.api_key = os.environ.get("VLLM_API_KEY") or None
    node.timeout_seconds = int(os.environ.get("VLLM_TIMEOUT_SECONDS", "600"))
    node.is_enabled = True
    node.is_default = True
    node.description = "Registered by deploy/native/c-check-deploy.sh"
    for mock in db.scalars(select(ModelNode).where(ModelNode.base_url.like("mock://%"))):
        mock.is_enabled = False
        mock.is_default = False
    db.commit()
PY
}

restart_services() {
  log "Restarting services"
  systemctl restart c-check-api c-check-worker
  systemctl reload nginx
}

install_all() {
  require_root "$@"
  load_env
  validate_env
  install_os_packages
  install_nodejs
  sync_source
  ensure_user_and_dirs
  write_app_env
  setup_database
  install_backend
  build_frontend
  write_systemd_units
  write_nginx_config
  register_vllm_model
  restart_services
  status
  log "Install complete. Open ${PUBLIC_ORIGIN}"
}

update_all() {
  require_root "$@"
  load_env
  validate_env
  sync_source
  write_app_env
  install_backend
  build_frontend
  register_vllm_model
  restart_services
  status
}

start() {
  require_root "$@"
  systemctl start mysql redis-server nginx c-check-api c-check-worker
}

stop() {
  require_root "$@"
  systemctl stop c-check-worker c-check-api || true
}

status() {
  load_env
  for service in c-check-vllm c-check-api c-check-worker nginx mysql redis-server; do
    printf '%s=' "${service}"
    systemctl is-active "${service}" 2>/dev/null || true
  done
  ss -ltnp | grep -E ":(${WEB_PORT}|${BACKEND_PORT}|8001) " || true
}

logs() {
  require_root "$@"
  local service="${2:-c-check-api}"
  journalctl -u "${service}" -n 200 -f
}

backup() {
  require_root "$@"
  load_env
  local backup_dir="${APP_DIR}/backups/$(date '+%F-%H%M%S')"
  mkdir -p "${backup_dir}"
  log "Backing up to ${backup_dir}"
  mysqldump --single-transaction --databases -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" "${MYSQL_DATABASE}" >"${backup_dir}/mysql.sql"
  tar czf "${backup_dir}/uploads.tgz" -C "${STORAGE_PATH}" .
  cp "${DEPLOY_ENV}" "${backup_dir}/deploy.env"
  cp "${APP_DIR}/.env" "${backup_dir}/app.env"
  log "Backup complete: ${backup_dir}"
}

usage() {
  cat <<EOF
Usage: sudo DEPLOY_ENV=/etc/c-check/c-check.env bash deploy/native/c-check-deploy.sh <command>

Commands:
  install   Install OS packages, app, database, Nginx, and services
  update    Pull latest code, migrate DB, rebuild frontend, restart services
  start     Start C-Check services
  stop      Stop API and worker
  status    Show service and port status
  logs      Follow a systemd service log, default c-check-api
  backup    Dump MySQL, uploads, and env files
EOF
}

case "${1:-}" in
  install) install_all "$@" ;;
  update) update_all "$@" ;;
  start) start "$@" ;;
  stop) stop "$@" ;;
  status) status ;;
  logs) logs "$@" ;;
  backup) backup "$@" ;;
  *) usage; exit 1 ;;
esac
