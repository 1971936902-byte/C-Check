"""Validate SPA deep links in an isolated, non-root, read-only frontend container."""
import argparse
import subprocess
import time
import uuid


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    image = run('docker', 'image', 'inspect', '--format', '{{.Id}}', args.image)
    name = 'ccheck-assistant-frontend-probe-' + uuid.uuid4().hex[:12]
    try:
        run('docker', 'run', '-d', '--rm', '--pull=never', '--name', name,
            '--network', 'none', '--add-host', 'api:127.0.0.1', '--runtime=runc',
            '--user=101:101', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--memory=256m', '--cpus=1', '--pids-limit=128',
            '--tmpfs', '/tmp:size=16777216,mode=1777',
            '--tmpfs', '/var/cache/nginx:size=16777216,uid=101,gid=101',
            '--tmpfs', '/var/run:size=1048576,uid=101,gid=101', image)
        for attempt in range(20):
            try:
                run('docker', 'exec', name, 'wget', '-q', '-O', '-', 'http://127.0.0.1:8080/')
                break
            except subprocess.CalledProcessError:
                if attempt == 19:
                    raise RuntimeError('isolated frontend did not become ready') from None
                time.sleep(1)
        for route in ('/assistant', '/assistant?conversation=probe', '/help'):
            page = run('docker', 'exec', name, 'wget', '-q', '-O', '-', 'http://127.0.0.1:8080' + route)
            assert '<!doctype html>' in page.lower() and '/assets/' in page
        assets = run('docker', 'exec', name, 'find', '/usr/share/nginx/html/assets', '-name', 'AssistantView-*.js')
        assert assets
        print('FRONTEND_IMAGE_DEEPLINK_PROBE_OK:', image)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
