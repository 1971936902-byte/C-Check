import os
from pathlib import Path
import sys

os.environ.setdefault('USER', 'ccheck')
os.environ.setdefault('LOGNAME', 'ccheck')
os.environ.setdefault('HOME', '/cache')
os.environ.setdefault('TORCHINDUCTOR_CACHE_DIR', '/cache/torchinductor')
key = Path(os.environ['VLLM_API_KEY_FILE']).read_text().rstrip('\r\n')
os.environ['VLLM_API_KEY'] = key
args = [sys.executable, '-m', 'vllm.entrypoints.openai.api_server',
        '--host', '0.0.0.0', '--port', '8001', '--model', os.environ['MODEL_PATH'],
        '--served-model-name', 'qwen2.5-coder-32b-instruct-fp8',
        '--dtype', 'bfloat16', '--kv-cache-dtype', 'auto', '--tensor-parallel-size', '1',
        '--gpu-memory-utilization', os.environ['GPU_MEMORY_UTILIZATION'], '--max-model-len', '8192',
        '--max-num-seqs', '1', '--max-num-batched-tokens', '4096', '--enable-chunked-prefill',
        '--enable-prefix-caching', '--enforce-eager', '--disable-log-requests']
os.execv(sys.executable, args)
