"""Isolated real-MySQL/real-Qwen scheduling probe, with inference restoration."""
import os
import secrets
import subprocess
import uuid

from probe_qwen_gpu import main, run


def check_gate(qwen, root, resources, network):
    name = 'ccheck-assistant-probe-' + uuid.uuid4().hex[:12]
    worker = name + '-worker'
    backend = run('docker','image','inspect','--format','{{.Id}}','ccheck-assistant-backend:development')
    mysql = run('docker','image','inspect','--format','{{.Id}}',os.environ['PROBE_MYSQL_IMAGE'])
    key = root / 'probe-mysql'
    key.write_text(secrets.token_hex(32))
    os.chown(key,10001,10001)
    key.chmod(0o400)
    try:
        run('docker','run','-d','--pull=never','--name',name,'--network',network,
            '--runtime=runc','--memory=1g','--cpus=2','--pids-limit=256',
            '--tmpfs','/var/lib/mysql:rw,size=805306368','--mount',
            f'type=bind,src={key},dst=/run/secrets/probe-mysql,readonly',
            '-e','MYSQL_ROOT_PASSWORD_FILE=/run/secrets/probe-mysql',
            '-e','MYSQL_PASSWORD_FILE=/run/secrets/probe-mysql','-e','MYSQL_USER=probe',
            '-e','MYSQL_DATABASE=ccheck_assistant_probe',mysql)
        command=['docker','run','--rm','--pull=never','--name',worker,'--network',network,
            '--runtime=runc','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
            '--user=10001:10001','--memory=1g','--cpus=2','--tmpfs','/tmp',
            '--mount',f'type=bind,src={resources},dst=/probe,readonly',
            '-e','PROBE_MYSQL_HOST='+name,'-e','NVIDIA_VISIBLE_DEVICES=void']
        for filename in ('probe-mysql','probe_api','probe_control'):
            command+=['--mount',f'type=bind,src={root / filename},dst=/run/secrets/{filename},readonly']
        print(run(*command,'--entrypoint','python',backend,'/probe/probe_real_gate.py',timeout=180),flush=True)
    finally:
        for container in (worker,name):
            subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=30)


if __name__ == '__main__':
    main(post_ready=check_gate)
