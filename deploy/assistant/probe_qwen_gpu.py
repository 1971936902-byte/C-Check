"""Real GPU cancellation probe; restore explicitly named inference containers."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid


CHECK = r'''
import asyncio, json, pathlib, secrets
import httpx
async def main():
    key=pathlib.Path('/run/secrets/probe_api').read_text().strip()
    control=pathlib.Path('/run/secrets/probe_control').read_text().strip()
    headers={'Authorization':'Bearer '+key,'X-CCheck-Control-Key':control}
    token=secrets.token_hex(32)
    path='/internal/assistant/'+token
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8001', headers=headers, timeout=60) as client:
        response=await client.post(path+'/reserve')
        response.raise_for_status()
        epoch=response.json()['epoch']
        body={'model':'qwen2.5-coder-32b-instruct-fp8',
              'messages':[{'role':'user','content':'Write a long numbered list of 500 C programming tips.'}],
              'request_id':'ccheck-assistant-'+token,'stream':True,'max_tokens':2048}
        saw_token=False
        async with client.stream('POST','/v1/chat/completions',json=body) as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if not line.startswith('data: ') or line=='data: [DONE]': continue
                event=json.loads(line[6:])
                if any(c.get('delta',{}).get('content') for c in event.get('choices',[])):
                    saw_token=True
                    response=await client.post(path+'/cancel',params={'epoch':epoch})
                    response.raise_for_status()
                    assert response.json().get('epoch')==epoch and response.json().get('idle') is True
                    break
        assert saw_token, 'real model produced no token before cancellation'
        response=await client.delete(path,params={'epoch':epoch})
        response.raise_for_status()
        response=await client.post('/v1/chat/completions',json={
            'model':body['model'],'messages':[{'role':'user','content':'Reply with OK.'}], 'max_tokens':8})
        response.raise_for_status()
        assert response.json()['choices'][0]['message']['content']
        print('QWEN_REAL_GPU_CANCEL_IDLE_AND_REVIEW_REQUEST_OK')
asyncio.run(main())
'''


def run(*args, timeout=30):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def wait_for_restored_inference(originals, snapshots, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        healthy = True
        for snapshot, name in zip(snapshots, originals):
            current = json.loads(run('docker', 'inspect', name))[0]
            if current['Id'] != snapshot['Id'] or not current['State']['Running']:
                raise RuntimeError('original inference container changed identity or stopped')
            if 'Health' in snapshot['State']:
                healthy = healthy and current['State'].get('Health', {}).get('Status') == 'healthy'
        if healthy:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('original inference restarted but health recovery timed out')
        time.sleep(3)


def main(*, workload='qwen', check=CHECK, post_ready=None):
    if workload not in {'qwen', 'jina'}:
        raise ValueError('unsupported probe workload')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qwen-container', required=True)
    parser.add_argument('--embedding-container', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--embedding-launcher', type=Path)
    parser.add_argument('--jina-model-dir', type=Path, help='Also mount Jina for the dual-residency probe')
    args = parser.parse_args()
    if (workload == 'jina' or args.jina_model_dir) and (args.embedding_launcher is None
            or not args.embedding_launcher.is_absolute() or not args.embedding_launcher.is_file()):
        parser.error('Jina probe requires an existing absolute --embedding-launcher')
    if args.jina_model_dir and (workload != 'qwen' or not args.jina_model_dir.is_absolute()
            or not (args.jina_model_dir / 'config.json').is_file()):
        parser.error('dual-residency probe requires a Qwen workload and existing absolute Jina directory')
    originals = [args.qwen_container, args.embedding_container]
    snapshots = [json.loads(run('docker', 'inspect', name))[0] for name in originals]
    if len(set(originals)) != 2 or not all(item['State']['Running'] for item in snapshots):
        raise RuntimeError('both distinct original inference containers must be running')
    image = run('docker', 'image', 'inspect', '--format', '{{.Id}}', args.image)
    if not args.model_dir.is_absolute() or not (args.model_dir / 'config.json').is_file():
        raise ValueError('missing model directory')
    if workload == 'qwen' and json.loads((args.model_dir / 'config.json').read_text())['model_type'] != 'qwen2':
        raise ValueError('this probe must not migrate to another model family')
    name = 'ccheck-' + workload + '-gpu-probe-' + uuid.uuid4().hex[:12]
    resources = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix=name) as temporary:
        root = Path(temporary)
        credentials = []
        for filename in ('probe_api', 'probe_control'):
            value = secrets.token_hex(32)
            credentials.append(value)
            path = root / filename
            path.write_text(value)
            os.chown(path, 10001, 10001)
            path.chmod(0o400)
        stopped = False
        probe_network = None
        try:
            if post_ready is not None:
                probe_network = name + '-net'
                run('docker', 'network', 'create', '--internal', probe_network)
            # Mark before stop: a partial stop failure still requires restoration.
            stopped = True
            run('docker', 'stop', '--time', '30', *originals, timeout=90)
            command = ['docker','run','-d','--pull=never','--name',name,'--network',probe_network or 'none',
                '--gpus','device=0','--user','10001:10001','--read-only','--cap-drop=ALL',
                '--security-opt=no-new-privileges:true','--shm-size=2g',
                '--tmpfs','/tmp:rw,exec,size=2147483648,mode=1777',
                '--tmpfs','/cache:rw,exec,size=2147483648,mode=1777',
                '--mount',f'type=bind,src={resources},dst=/opt/assistant,readonly',
                '--mount',f'type=bind,src={args.model_dir},dst=/models/{workload},readonly']
            if probe_network:
                command += ['--network-alias','probe-qwen']
            if workload == 'jina' or args.jina_model_dir:
                command += ['--mount',f'type=bind,src={args.embedding_launcher},dst=/opt/ccheck-runtime/ccheck_embedding.py,readonly']
            if args.jina_model_dir:
                command += ['--mount',f'type=bind,src={args.jina_model_dir},dst=/models/jina,readonly']
            for filename in ('probe_api','probe_control'):
                command += ['--mount',f'type=bind,src={root / filename},dst=/run/secrets/{filename},readonly']
            env = {'MODEL_PATH':'/models/qwen','GPU_MEMORY_UTILIZATION':'0.94',
                   'MAX_NUM_SEQS':'4','MAX_NUM_BATCHED_TOKENS':'8192',
                   'VLLM_API_KEY_FILE':'/run/secrets/probe_api',
                   'ASSISTANT_CONTROL_KEY_FILE':'/run/secrets/probe_control',
                   'HOME':'/tmp','HF_HOME':'/tmp/hf','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
                   'VLLM_CACHE_ROOT':'/tmp/vllm','PYTHONDONTWRITEBYTECODE':'1'}
            if workload == 'jina':
                env.update(MODEL_PATH='/models/jina', RAG_EMBEDDING_API_KEY_FILE='/run/secrets/probe_api',
                           EMBEDDING_BATCH_SIZE='1', EMBEDDING_TOKEN_BUDGET='8192')
            if args.jina_model_dir:
                # A100 40GB test budget only; target 48GB deployment settings are unchanged.
                env.update(GPU_MEMORY_UTILIZATION='0.88', MAX_NUM_SEQS='1', MAX_NUM_BATCHED_TOKENS='1024')
            for key,value in env.items(): command += ['-e',key+'='+value]
            launcher = 'qwen_assistant_gpu.py' if workload == 'qwen' else 'embedding_assistant_gpu.py'
            port = 8001 if workload == 'qwen' else 8002
            command += ['--entrypoint','python',image,'/opt/assistant/' + launcher]
            run(*command)
            deadline = time.monotonic() + 300
            while True:
                try:
                    run('docker','exec',name,'python','-c',
                        f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/health',timeout=3)", timeout=10)
                    break
                except subprocess.CalledProcessError:
                    if run('docker','inspect','--format','{{.State.Running}}',name) != 'true':
                        raise RuntimeError('test inference exited') from None
                    if time.monotonic() >= deadline: raise RuntimeError('test inference readiness timed out')
                    time.sleep(3)
            print(run('docker','exec',name,'python','-c',check,timeout=120),flush=True)
            if post_ready is not None:
                post_ready(name, root, resources, probe_network)
        except Exception as error:
            logs = subprocess.run(['docker','logs','--tail','300',name],capture_output=True,text=True,timeout=15)
            diagnostic = logs.stdout + logs.stderr + str(getattr(error,'stdout','')) + str(getattr(error,'stderr',''))
            for value in credentials: diagnostic = diagnostic.replace(value,'[redacted]')
            print(diagnostic[-60000:],flush=True)
            raise RuntimeError('GPU probe failed; see redacted diagnostics') from None
        finally:
            # Never restore originals while the test may still own GPU memory.
            removed = subprocess.run(['docker','rm','-f',name],capture_output=True,text=True,timeout=45)
            remaining = subprocess.run(['docker','inspect',name],capture_output=True,timeout=15)
            if remaining.returncode == 0:
                raise RuntimeError('test container remains; manual restoration required')
            if stopped:
                run('docker','start',*originals,timeout=60)
                wait_for_restored_inference(originals, snapshots)
                print('ORIGINAL_INFERENCE_CONTAINERS_RESTARTED_SAME_IDS',flush=True)
                print('ORIGINAL_INFERENCE_HEALTH_RECOVERY_CONFIRMED',flush=True)
            if probe_network:
                run('docker','network','rm',probe_network)


if __name__ == '__main__':
    main()
