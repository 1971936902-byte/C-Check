# VLLM Model Deployment Changelog - 2026-06-04

## Scope

This record covers the switch from the built-in mock model to a real GPU-backed VLLM model on the new cloud server.

## Server

- SSH endpoint: `180.127.11.167:24116`
- Public web endpoint: `http://180.127.11.167:24164/`
- Internal web port: `8800`
- App path: `/opt/c-check`
- VLLM environment: `/opt/vllm`
- Model path: `/opt/models/Qwen2.5-Coder-32B-Instruct-AWQ`

## Model Service

- Model source: ModelScope
- Model: `Qwen/Qwen2.5-Coder-32B-Instruct-AWQ`
- Served model name: `qwen2.5-coder-32b-awq`
- VLLM endpoint: `http://127.0.0.1:8001`
- Systemd service: `c-check-vllm`
- Final stable launch choices:
  - `--quantization awq_marlin`
  - `--dtype half`
  - `--tensor-parallel-size 1`
  - `--gpu-memory-utilization 0.82`
  - `--max-model-len 4096`
  - `--enforce-eager`

## Issues Fixed

1. Hugging Face download was rate-limited, so the model was downloaded from ModelScope.
2. `transformers 5.x` caused tokenizer compatibility errors with VLLM, so the runtime was pinned to a VLLM-compatible `transformers 4.56.2` stack.
3. `numpy 2.4.x` was incompatible with `numba`, so it was pinned to `numpy 2.2.6`.
4. Torch Inductor/Triton compilation failed during VLLM profiling on this image. The service was relaunched with `--enforce-eager`.
5. The initial 8192 context configuration was reduced to 4096 for stable single A100 40GB operation.
6. A temporary service file interpolation mistake wrote a literal `{api_key}` as the VLLM API key. The service was rewritten with the actual key from `/opt/c-check/.vllm.env`.

## Backend Switch

- Added/updated the C-Check model node:
  - Display name: `Qwen2.5 Coder 32B AWQ`
  - Identifier: `qwen2.5-coder-32b-awq`
  - Base URL: `http://127.0.0.1:8001`
  - Timeout: `600`
  - Enabled: `true`
  - Default: `true`
- Disabled the old `mock://local` model node.
- Updated `/opt/c-check/.env`:
  - `MOCK_MODEL_ENABLED=false`
  - CORS origins include the new public IP and mapped port.
- Restarted:
  - `c-check-api`
  - `c-check-worker`

## Verification

Completed checks:

- `c-check-vllm`, `c-check-api`, `c-check-worker`, `nginx`, `mysql`, and `redis-server` were all active.
- Nginx was listening on `0.0.0.0:8800`.
- API was listening on `127.0.0.1:8000`.
- VLLM was listening on `127.0.0.1:8001`.
- `GET /v1/models` returned `qwen2.5-coder-32b-awq`.
- GPU memory after model load was about `33743 MiB / 40960 MiB`.
- Admin login API returned `200`.
- `/api/models?default_only=true` returned the real Qwen model node.
- `/api/models/{model_id}/health` returned `{"ok": true}`.
- A real C code review task completed successfully:
  - Input: `strcpy` into a fixed-size buffer.
  - Final status: `completed`.
  - Findings: `3`.
  - Report was readable through `/api/reports/{report_id}`.

## Current Access

Use the new public URL:

```text
http://180.127.11.167:24164/
```

The old local URL `http://127.0.0.1:18000/` belongs to the previous tunnel-based setup and is not the active public entry for this deployment.
