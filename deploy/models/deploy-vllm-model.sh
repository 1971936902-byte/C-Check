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
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$HOME/.cache/huggingface}"

command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }

if [[ "$SOURCE" == "modelscope" ]]; then
  MODEL_CACHE_DIR="${MODELSCOPE_CACHE:-$HOME/.cache/modelscope}"
fi

mkdir -p "$MODEL_CACHE_DIR"

if docker inspect "$SERVICE_NAME" >/dev/null 2>&1; then
  echo "Container $SERVICE_NAME already exists. Remove it before replacing the model service." >&2
  exit 1
fi

MODEL_ARG="$REPOSITORY"
if [[ "$SOURCE" == "local" ]]; then
  [[ -n "$MODEL_DIR" ]] || { echo "--model-dir is required for local source" >&2; exit 2; }
  MODEL_ARG="$MODEL_DIR"
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
  --api-key "$VLLM_API_KEY" \
  --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --max-model-len "$MAX_MODEL_LEN"

echo "Container started. Check readiness:"
echo "curl -fsS -H 'Authorization: Bearer *****' ${BASE_URL%/}/v1/models"
