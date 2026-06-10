#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE="huggingface"
REPOSITORY=""
SERVED_MODEL_NAME=""
BASE_URL=""
PORT=""
SERVICE_NAME=""
MODEL_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --repository) REPOSITORY="$2"; shift 2 ;;
    --served-model-name) SERVED_MODEL_NAME="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    --model-dir) MODEL_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$REPOSITORY" ]] || { echo "--repository is required" >&2; exit 2; }
[[ -n "$SERVED_MODEL_NAME" ]] || { echo "--served-model-name is required" >&2; exit 2; }
[[ -n "$PORT" ]] || { echo "--port is required" >&2; exit 2; }
[[ -n "$SERVICE_NAME" ]] || SERVICE_NAME="c-check-vllm-${SERVED_MODEL_NAME//\//-}"

VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"
VLLM_API_KEY="${VLLM_API_KEY:-}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-${HOME:-/data}/.cache/huggingface}"
MODEL_DEPLOYMENT_MODE="${MODEL_DEPLOYMENT_MODE:-auto}"
VLLM_PYTHON="${VLLM_PYTHON:-/opt/vllm/bin/python}"
STOP_EXISTING_VLLM="${STOP_EXISTING_VLLM:-true}"
READINESS_TIMEOUT_SECONDS="${READINESS_TIMEOUT_SECONDS:-1800}"

if [[ "$MODEL_DEPLOYMENT_MODE" == "auto" ]]; then
  if command -v docker >/dev/null 2>&1; then
    MODEL_DEPLOYMENT_MODE="docker"
  else
    MODEL_DEPLOYMENT_MODE="native"
  fi
fi

if [[ "$SOURCE" == "modelscope" ]]; then
  MODEL_CACHE_DIR="${MODELSCOPE_CACHE:-$HOME/.cache/modelscope}"
fi

mkdir -p "$MODEL_CACHE_DIR"

if [[ "$MODEL_DEPLOYMENT_MODE" == "docker" ]] && docker inspect "$SERVICE_NAME" >/dev/null 2>&1; then
  echo "Container $SERVICE_NAME already exists. Remove it before replacing the model service." >&2
  exit 1
fi

MODEL_ARG="$REPOSITORY"
if [[ "$SOURCE" == "local" ]]; then
  [[ -n "$MODEL_DIR" ]] || { echo "--model-dir is required for local source" >&2; exit 2; }
  MODEL_ARG="$MODEL_DIR"
fi

if [[ "$MODEL_DEPLOYMENT_MODE" == "native" ]]; then
  [[ -x "$VLLM_PYTHON" ]] || { echo "VLLM python not found or not executable: $VLLM_PYTHON" >&2; exit 1; }
  command -v systemctl >/dev/null 2>&1 || { echo "systemctl is required for native deployment." >&2; exit 1; }

  DOWNLOAD_DIR="$MODEL_CACHE_DIR"
  if [[ "$SOURCE" == "modelscope" ]]; then
    DOWNLOAD_DIR="${MODELSCOPE_CACHE:-/data/modelscope}"
  elif [[ "$SOURCE" == "huggingface" ]]; then
    DOWNLOAD_DIR="${HF_HOME:-/data/huggingface}"
  fi
  mkdir -p "$DOWNLOAD_DIR"

  if [[ "$SOURCE" == "huggingface" ]]; then
    echo "Downloading HuggingFace snapshot for $MODEL_ARG into $DOWNLOAD_DIR before switching services."
    "$VLLM_PYTHON" - "$MODEL_ARG" "$DOWNLOAD_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download

repo_id, cache_dir = sys.argv[1], sys.argv[2]
snapshot_download(repo_id=repo_id, cache_dir=cache_dir, resume_download=True)
PY
  elif [[ "$SOURCE" == "modelscope" ]]; then
    echo "Downloading ModelScope snapshot for $MODEL_ARG into $DOWNLOAD_DIR before switching services."
    "$VLLM_PYTHON" - "$MODEL_ARG" "$DOWNLOAD_DIR" <<'PY'
import sys
from modelscope import snapshot_download

model_id, cache_dir = sys.argv[1], sys.argv[2]
snapshot_download(model_id, cache_dir=cache_dir)
PY
  fi

  if [[ "$STOP_EXISTING_VLLM" == "true" ]]; then
    while read -r unit _; do
      [[ -n "$unit" ]] || continue
      [[ "$unit" == "${SERVICE_NAME}.service" ]] && continue
      echo "Stopping existing VLLM service $unit to free GPU memory."
      systemctl stop "$unit" || true
      systemctl disable "$unit" || true
    done < <(systemctl list-units --type=service --all 'c-check-vllm*.service' --no-legend --no-pager || true)
  fi

  API_KEY_FRAGMENT=""
  if [[ -n "$VLLM_API_KEY" ]]; then
    API_KEY_FRAGMENT="--api-key ${VLLM_API_KEY}"
  fi

  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  echo "Writing native systemd service $SERVICE_NAME for $MODEL_ARG on port $PORT"
  cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=C-Check vLLM OpenAI API (${SERVED_MODEL_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-0}
Environment=VLLM_USE_MODELSCOPE=$([[ "$SOURCE" == "modelscope" ]] && echo true || echo false)
Environment=HF_HOME=${HF_HOME:-/data/huggingface}
Environment=MODELSCOPE_CACHE=${MODELSCOPE_CACHE:-/data/modelscope}
WorkingDirectory=/opt/c-check
ExecStart=${VLLM_PYTHON} -m vllm.entrypoints.openai.api_server --host 127.0.0.1 --port ${PORT} --model ${MODEL_ARG} --served-model-name ${SERVED_MODEL_NAME} ${API_KEY_FRAGMENT} --tensor-parallel-size ${TENSOR_PARALLEL_SIZE} --gpu-memory-utilization ${GPU_MEMORY_UTILIZATION} --max-model-len ${MAX_MODEL_LEN} --download-dir ${DOWNLOAD_DIR} --enforce-eager
Restart=on-failure
RestartSec=10
TimeoutStartSec=0
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME"

  echo "Waiting for readiness: ${BASE_URL%/}/v1/models"
  deadline=$((SECONDS + READINESS_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if curl -fsS -H "Authorization: Bearer ${VLLM_API_KEY}" "${BASE_URL%/}/v1/models" >/dev/null 2>&1; then
      echo "Native VLLM service is ready."
      exit 0
    fi
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
      echo "Native VLLM service stopped before readiness." >&2
      journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
      exit 1
    fi
    sleep 10
  done

  echo "Timed out waiting for native VLLM readiness." >&2
  journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
  exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }
DOCKER_API_KEY_ARGS=()
if [[ -n "$VLLM_API_KEY" ]]; then
  DOCKER_API_KEY_ARGS=(--api-key "$VLLM_API_KEY")
fi

echo "Starting VLLM container $SERVICE_NAME for $MODEL_ARG on port $PORT"

docker run -d \
  --name "$SERVICE_NAME" \
  --restart unless-stopped \
  --gpus all \
  --ipc=host \
  -p "${PORT}:8000" \
  -e "HF_TOKEN=${HF_TOKEN:-}" \
  -e "VLLM_USE_MODELSCOPE=${VLLM_USE_MODELSCOPE:-$([[ "$SOURCE" == "modelscope" ]] && echo true || echo false)}" \
  -v "${MODEL_CACHE_DIR}:/root/.cache/huggingface" \
  "$VLLM_IMAGE" \
  --model "$MODEL_ARG" \
  --served-model-name "$SERVED_MODEL_NAME" \
  "${DOCKER_API_KEY_ARGS[@]}" \
  --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --max-model-len "$MAX_MODEL_LEN"

echo "Container started. Check readiness:"
echo "curl -fsS -H 'Authorization: Bearer *****' ${BASE_URL%/}/v1/models"
