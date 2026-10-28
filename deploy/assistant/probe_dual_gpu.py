"""Verify real Qwen/Jina GPU co-residency, not full deployment orchestration."""
from probe_qwen_gpu import main


CHECK = r'''
import os, subprocess, sys, time, urllib.request
sys.path.insert(0, '/opt/assistant')
from probe_qwen_gpu import CHECK as qwen_check
from probe_jina_gpu import CHECK as jina_check
env = dict(os.environ, MODEL_PATH='/models/jina',
           RAG_EMBEDDING_API_KEY_FILE='/run/secrets/probe_api',
           EMBEDDING_BATCH_SIZE='1', EMBEDDING_TOKEN_BUDGET='8192')
# Separate model processes share the same GPU; no second Qwen is loaded.
with open('/tmp/jina-probe.log','w') as log:
    child = subprocess.Popen([sys.executable,'/opt/assistant/embedding_assistant_gpu.py'],
                             env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        for attempt in range(60):
            try:
                urllib.request.urlopen('http://127.0.0.1:8002/health',timeout=2).close()
                break
            except Exception:
                if child.poll() is not None: raise RuntimeError('Jina startup exited')
                time.sleep(1)
        else: raise RuntimeError('Jina readiness timed out')
        exec(qwen_check, {})
        exec(jina_check, {})
        print('QWEN_JINA_REAL_GPU_CORESIDENCY_OK')
    except Exception:
        log.flush()
        print(open('/tmp/jina-probe.log').read()[-10000:])
        raise
    finally:
        child.terminate()
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
'''


if __name__ == '__main__':
    main(check=CHECK)
