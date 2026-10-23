import os
from pathlib import Path
import sys

os.environ['VLLM_API_KEY'] = Path(os.environ['VLLM_API_KEY_FILE']).read_text().rstrip('\r\n')
args = [sys.executable, '-m', 'vllm.entrypoints.openai.api_server',
        '--host', '0.0.0.0', '--port', '8001', '--model', os.environ['MODEL_PATH'],
        '--served-model-name', 'qwen3-coder-30b-a3b-instruct-fp8',
        '--dtype', 'bfloat16', '--kv-cache-dtype', 'auto', '--tensor-parallel-size', '1',
        '--gpu-memory-utilization', os.environ['GPU_MEMORY_UTILIZATION'], '--max-model-len', '8192',
        '--max-num-seqs', os.environ['MAX_NUM_SEQS'],
        '--max-num-batched-tokens', os.environ['MAX_NUM_BATCHED_TOKENS'],
        '--enable-chunked-prefill', '--enable-prefix-caching', '--enforce-eager', '--disable-log-requests']
os.execv(sys.executable, args)
