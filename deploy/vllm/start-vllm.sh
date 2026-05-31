#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

[[ -f .env ]] || {
  echo "Missing deploy/vllm/.env. Copy .env.example to .env and configure it." >&2
  exit 1
}

set -a
# shellcheck disable=SC1091
source .env
set +a

required_vars=(
  VLLM_IMAGE
  VLLM_CONTAINER_NAME
  MODEL_ID
  SERVED_MODEL_NAME
  HF_TOKEN
  VLLM_API_KEY
  VLLM_PORT
  TENSOR_PARALLEL_SIZE
  GPU_MEMORY_UTILIZATION
  MAX_MODEL_LEN
)

for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" || "${!var_name}" == *CHANGE_ME* ]]; then
    echo "Set a non-placeholder value for ${var_name} in deploy/vllm/.env." >&2
    exit 1
  fi
done

command -v docker >/dev/null 2>&1 || {
  echo "Docker with NVIDIA Container Toolkit support is required." >&2
  exit 1
}

if docker inspect "$VLLM_CONTAINER_NAME" >/dev/null 2>&1; then
  echo "Container ${VLLM_CONTAINER_NAME} already exists. Remove it explicitly before replacing it." >&2
  exit 1
fi

docker run -d \
  --name "$VLLM_CONTAINER_NAME" \
  --restart unless-stopped \
  --gpus all \
  --ipc=host \
  -p "${VLLM_PORT}:8000" \
  -e "HF_TOKEN=${HF_TOKEN}" \
  -v "${HOME}/.cache/huggingface:/root/.cache/huggingface" \
  "$VLLM_IMAGE" \
  --model "$MODEL_ID" \
  --served-model-name "$SERVED_MODEL_NAME" \
  --api-key "$VLLM_API_KEY" \
  --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --max-model-len "$MAX_MODEL_LEN"

echo "Started ${VLLM_CONTAINER_NAME}. Check readiness with:"
echo "curl -fsS -H 'Authorization: Bearer ${VLLM_API_KEY}' http://127.0.0.1:${VLLM_PORT}/v1/models"
