"""Optional assistant-aware launcher for the existing Qwen2.5 FP8 image."""
import os
from pathlib import Path


def arguments(env):
    return [
        '--host', '0.0.0.0', '--port', '8001', '--model', env['MODEL_PATH'],
        '--served-model-name', 'qwen2.5-coder-32b-instruct-fp8',
        '--dtype', 'bfloat16', '--kv-cache-dtype', 'auto',
        '--tensor-parallel-size', '1', '--pipeline-parallel-size', '1',
        '--gpu-memory-utilization', env['GPU_MEMORY_UTILIZATION'],
        '--max-model-len', '8192', '--max-num-seqs', env['MAX_NUM_SEQS'],
        '--max-num-batched-tokens', env['MAX_NUM_BATCHED_TOKENS'],
        '--enable-chunked-prefill', '--enable-prefix-caching', '--enforce-eager',
        '--disable-log-requests', '--disable-frontend-multiprocessing',
        '--guided-decoding-backend', 'guidance',
    ]


def main():
    os.environ['VLLM_USE_V1'] = '0'
    os.environ['VLLM_API_KEY'] = Path(os.environ['VLLM_API_KEY_FILE']).read_text().rstrip('\r\n')
    key = Path(os.environ['ASSISTANT_CONTROL_KEY_FILE']).read_text().rstrip('\r\n')
    from vllm.entrypoints.openai import api_server
    from vllm_control import install
    import uvloop

    api_server.cli_env_setup()
    parser = api_server.make_arg_parser(api_server.FlexibleArgumentParser())
    args = parser.parse_args(arguments(os.environ))
    api_server.validate_parsed_serve_args(args)
    install(api_server, key)
    from vllm.model_executor.guided_decoding import guidance_decoding
    from guidance_compat import install as install_guidance_compat
    install_guidance_compat(guidance_decoding)
    uvloop.run(api_server.run_server(args))


if __name__ == '__main__':
    main()
