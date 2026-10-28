"""Disposable real-model RAG stack; never touches the existing database."""
import os
import secrets
import subprocess
import time
import uuid

from prepare_storage import prepare
from probe_qwen_gpu import main, run


def check_rag(qwen,root,resources,network):
    name='ccheck-assistant-probe-'+uuid.uuid4().hex[:12]
    mysql,gateway,provider,worker=[name+'-'+part for part in ('mysql','gateway','provider','worker')]
    redis,api,celery=[name+'-'+part for part in ('redis','api','celery')]
    review_worker=name+'-review-worker'
    preemption=os.environ.get('PROBE_REVIEW_PREEMPTION')=='1'
    public=os.environ.get('PROBE_PUBLIC_RAG')=='1'
    backend=run('docker','image','inspect','--format','{{.Id}}','ccheck-assistant-backend:development')
    provider_image=run('docker','image','inspect','--format','{{.Id}}','ccheck-anythingllm:verified-development')
    mysql_image=run('docker','image','inspect','--format','{{.Id}}',os.environ['PROBE_MYSQL_IMAGE'])
    redis_image=run('docker','image','inspect','--format','{{.Id}}',os.environ['PROBE_REDIS_IMAGE']) if public else None
    prepare(root)
    password=secrets.token_hex(32)
    values={'probe-mysql':password,'probe_db':f'mysql+pymysql://probe:{password}@{mysql}/ccheck_assistant_probe',
            'probe_jwt':secrets.token_hex(32),'probe_admin':secrets.token_hex(32)}
    for key,value in values.items():
        path=root/key
        path.write_text(value)
        os.chown(path,10001,10001)
        path.chmod(0o400)
    env={'DATABASE_URL_FILE':'/run/secrets/probe_db','JWT_SECRET_FILE':'/run/secrets/probe_jwt',
        'ADMIN_PASSWORD_FILE':'/run/secrets/probe_admin','ASSISTANT_ENABLED':'true','STORAGE_PATH':'/tmp/storage',
        'ASSISTANT_GATEWAY_KEY_FILE':'/run/secrets/assistant_gateway_key',
        'ASSISTANT_CONTROL_KEY_FILE':'/run/secrets/probe_control',
        'ASSISTANT_QWEN_API_KEY_FILE':'/run/secrets/probe_api','ASSISTANT_QWEN_BASE_URL':'http://probe-qwen:8001',
        'RAG_EMBEDDING_BASE_URL':'http://probe-qwen:8002/v1','RAG_EMBEDDING_MODEL':'jina-code-embeddings-1.5b',
        'RAG_EMBEDDING_API_KEY_FILE':'/run/secrets/probe_api',
        'ASSISTANT_ANYTHINGLLM_URL':'http://probe-provider:3001',
        'ASSISTANT_ANYTHINGLLM_API_KEY_FILE':'/run/secrets/assistant_anythingllm_api_key'}
    common=['--pull=never','--network',network,'--runtime=runc','--read-only','--cap-drop=ALL',
        '--security-opt=no-new-privileges:true','--user=10001:10001','--tmpfs','/tmp:rw,exec,mode=1777',
        '--mount',f'type=bind,src={resources},dst=/opt/assistant,readonly','-e','NVIDIA_VISIBLE_DEVICES=void']
    for key in ('probe_db','probe_jwt','probe_admin','probe_api','probe_control'):
        common+=['--mount',f'type=bind,src={root/key},dst=/run/secrets/{key},readonly']
    for key in ('assistant_gateway_key','assistant_anythingllm_api_key'):
        common+=['--mount',f'type=bind,src={root / "secrets" / key},dst=/run/secrets/{key},readonly']
    backend_args=list(common)
    if public:
        storage=root/'public-storage'
        storage.mkdir(mode=0o700)
        os.chown(storage,10001,10001)
        backend_args+=['--mount',f'type=bind,src={storage},dst=/probe-storage']
        env.update(STORAGE_PATH='/probe-storage',REDIS_URL=f'redis://{redis}:6379/0')
        if preemption: env['PROBE_REVIEW_PREEMPTION']='1'
    for key,value in env.items(): backend_args+=['-e',key+'='+value]
    try:
        run('docker','exec','-d','-e','MODEL_PATH=/models/jina',
            '-e','RAG_EMBEDDING_API_KEY_FILE=/run/secrets/probe_api',qwen,
            'python','/opt/assistant/embedding_assistant_gpu.py')
        for attempt in range(60):
            try:
                run('docker','exec',qwen,'python','-c',
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8002/health',timeout=2)")
                break
            except subprocess.CalledProcessError:
                if attempt==59: raise
                time.sleep(1)
        run('docker','run','-d','--pull=never','--name',mysql,'--network',network,'--runtime=runc',
            '--memory=1g','--tmpfs','/var/lib/mysql:rw,size=805306368',
            '--mount',f'type=bind,src={root / "probe-mysql"},dst=/run/secrets/probe-mysql,readonly',
            '-e','MYSQL_ROOT_PASSWORD_FILE=/run/secrets/probe-mysql','-e','MYSQL_PASSWORD_FILE=/run/secrets/probe-mysql',
            '-e','MYSQL_USER=probe','-e','MYSQL_DATABASE=ccheck_assistant_probe',mysql_image)
        print(run('docker','run','--rm','--name',worker,*backend_args,'--entrypoint','python',backend,
            '/opt/assistant/probe_real_rag.py','--initialize',timeout=100),flush=True)
        run('docker','run','-d','--name',gateway,'--network-alias','assistant-gateway',*backend_args,
            '--entrypoint','uvicorn',backend,'app.assistant_gateway:app','--host','0.0.0.0','--port','8010')
        mounts=[]
        for source,target in [('storage','/app/server/storage'),('collector-hotdir','/app/collector/hotdir'),
            ('collector-storage','/app/collector/storage'),('server.env','/app/server/.env')]:
            mounts+=['--mount',f'type=bind,src={root / "data/assistant" / source},dst={target}']
        run('docker','run','-d','--name',provider,'--network-alias','probe-provider',*common,*mounts,
            '--env-file',str(resources/'anythingllm.env.example'),'-e','HOME=/tmp',
            '-e','ASSISTANT_GATEWAY_KEY_FILE=/run/secrets/assistant_gateway_key',
            '-e','ASSISTANT_ANYTHINGLLM_API_KEY_FILE=/run/secrets/assistant_anythingllm_api_key',provider_image)
        for attempt in range(60):
            try:
                run('docker','exec',provider,'node','-e',
                    "Promise.all(['http://127.0.0.1:3001/api/ping','http://127.0.0.1:8888/accepts','http://assistant-gateway:8010/health'].map(u=>fetch(u,{signal:AbortSignal.timeout(3000)}))).then(rs=>{if(rs.some(r=>!r.ok))process.exit(1)}).catch(()=>process.exit(1))")
                break
            except subprocess.CalledProcessError:
                if attempt==59: raise
                time.sleep(1)
        if public:
            run('docker','run','-d','--pull=never','--name',redis,'--network',network,'--runtime=runc',
                '--memory=256m','--tmpfs','/data',redis_image,'redis-server','--save','','--appendonly','no')
            run('docker','run','-d','--name',celery,*backend_args,'--entrypoint','celery',backend,
                '-A','app.worker.celery_app','worker','--loglevel=INFO','--concurrency=1',
                '--prefetch-multiplier=1','--queues=assistant')
            run('docker','run','-d','--name',api,'--network-alias','probe-api',*backend_args,
                '--entrypoint','uvicorn',backend,'app.main:app','--host','0.0.0.0','--port','8000')
            if preemption:
                run('docker','run','-d','--name',review_worker,*backend_args,'--entrypoint','celery',backend,
                    '-A','app.worker.celery_app','worker','--loglevel=INFO','--concurrency=1',
                    '--prefetch-multiplier=1','--queues=celery')
        script='probe_public_rag.py' if public else 'probe_real_rag.py'
        print(run('docker','run','--rm','--name',worker,*backend_args,'--entrypoint','python',backend,
            '/opt/assistant/'+script,timeout=420),flush=True)
    except Exception as error:
        diagnostic=str(getattr(error,'stdout',''))+str(getattr(error,'stderr',''))
        for container in ((gateway,provider,api,celery,redis,review_worker) if public else (gateway,provider)):
            result=subprocess.run(['docker','logs','--tail','35',container],capture_output=True,text=True,timeout=15)
            diagnostic+='\n'+container+'\n'+result.stdout+result.stderr
        for path in [root/key for key in (*values,'probe_api','probe_control')]+list((root/'secrets').iterdir()):
            value=path.read_text().strip()
            if value: diagnostic=diagnostic.replace(value,'[redacted]')
        print(diagnostic[-20000:],flush=True)
        raise RuntimeError('real RAG stack probe failed') from None
    finally:
        for container in (worker,api,celery,review_worker,redis,provider,gateway,mysql):
            subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=30)


if __name__=='__main__':
    main(post_ready=check_rag)
