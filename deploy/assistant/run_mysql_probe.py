"""Create and remove an isolated, CPU-only MySQL test environment using cached images."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import uuid


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--backend-image', required=True)
    parser.add_argument('--mysql-image', required=True)
    parser.add_argument('--mode', choices=['gate', 'backend'], default='gate')
    args = parser.parse_args()
    # Resolve local image IDs first; this probe never pulls images implicitly.
    images = [run('docker', 'image', 'inspect', '--format', '{{.Id}}', value, capture=True)
              for value in (args.backend_image, args.mysql_image)]
    name = 'ccheck-assistant-probe-' + uuid.uuid4().hex[:12]
    network, worker = name + '-net', name + '-worker'
    with tempfile.TemporaryDirectory(prefix=name) as temporary:
        key = Path(temporary) / 'password'
        key.write_text(secrets.token_hex(32))
        os.chown(key, 10001, 10001)
        key.chmod(0o400)
        network_created = False
        try:
            run('docker', 'network', 'create', '--internal', network)
            network_created = True
            run('docker', 'run', '-d', '--pull=never', '--name', name, '--network', network,
                '--runtime=runc', '--memory=1g', '--cpus=2', '--pids-limit=256',
                '--tmpfs', '/var/lib/mysql:rw,size=805306368',
                '--mount', f'type=bind,src={key},dst=/run/secrets/probe-mysql,readonly',
                '-e', 'MYSQL_ROOT_PASSWORD_FILE=/run/secrets/probe-mysql',
                '-e', 'MYSQL_PASSWORD_FILE=/run/secrets/probe-mysql',
                '-e', 'MYSQL_USER=probe', '-e', 'MYSQL_DATABASE=ccheck_assistant_probe', images[1])
            run('docker', 'run', '--rm', '--pull=never', '--name', worker, '--network', network,
                '--runtime=runc', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                '--memory=1g', '--cpus=2', '--pids-limit=256', '--user=10001:10001', '--tmpfs', '/tmp',
                '--mount', f'type=bind,src={args.source.resolve()},dst=/probe,readonly',
                '--mount', f'type=bind,src={key},dst=/run/secrets/probe-mysql,readonly',
                '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=' + (
                    '/probe/backend' if args.mode == 'gate' else '/opt/c-check-assistant/backend'),
                '-e', 'NVIDIA_VISIBLE_DEVICES=void', '-e', 'PROBE_MYSQL_HOST=' + name,
                '--entrypoint', 'python', images[0], '/probe/deploy/assistant/' + (
                    'probe_mysql_gate.py' if args.mode == 'gate' else 'probe_backend_image.py'))
        finally:
            # Names are generated above; no shared container or volume is targeted.
            for container in (worker, name):
                subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if network_created:
                run('docker', 'network', 'rm', network)


if __name__ == '__main__':
    main()
