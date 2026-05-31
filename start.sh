#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "Docker Compose is required (docker compose or docker-compose)." >&2
    exit 1
  fi
}

require_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "Docker is required." >&2
    exit 1
  }
}

require_runtime_env() {
  [[ -f .env ]] || {
    echo "Missing .env. Run ./start.sh install, then replace every CHANGE_ME value." >&2
    exit 1
  }
  if grep -q 'CHANGE_ME' .env; then
    echo ".env still contains CHANGE_ME placeholders. Refusing to start." >&2
    exit 1
  fi
}

install() {
  require_docker
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example."
  else
    echo ".env already exists; leaving it unchanged."
  fi
  echo "Edit .env and replace every CHANGE_ME value, then run ./start.sh start."
}

start() {
  require_docker
  require_runtime_env
  compose config --quiet
  compose up -d --build
  compose ps
}

stop() {
  require_docker
  compose down
}

status() {
  require_docker
  compose ps
}

logs() {
  require_docker
  shift || true
  compose logs --tail=200 -f "$@"
}

usage() {
  echo "Usage: ./start.sh {install|start|stop|status|logs [service...]}"
}

case "${1:-}" in
  install) install ;;
  start) start ;;
  stop) stop ;;
  status) status ;;
  logs) logs "$@" ;;
  *) usage; exit 1 ;;
esac
