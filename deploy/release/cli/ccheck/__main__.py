import argparse
import json
import os
from pathlib import Path
import sys

from .core import DeploymentError, installation_lock, load_site
from .engine import Engine, doctor
from . import lifecycle


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog='ccheck', description='C-Check signed release deployment manager')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['doctor', 'install', 'status', 'version', 'start', 'stop', 'model-start', 'model-stop', 'verify',
                 'logs', 'backup', 'restore', 'update', 'rollback', 'publish', 'uninstall', 'network-plan', 'network-recover']:
        command = sub.add_parser(name)
        command.add_argument('--config', required=True, type=Path, help='site YAML/JSON configuration')
        if name in {'install', 'update', 'rollback'}:
            command.add_argument('--release', type=Path, default=Path(sys.argv[0]).resolve().parent.parent)
            command.add_argument('--version')
            command.add_argument('--trusted-key', type=Path)
        if name == 'install':
            command.add_argument('--runtime-source', choices=['image', 'build'])
            command.add_argument('--backup-key', type=Path)
        if name in {'install', 'update'}:
            command.add_argument('--gpu-access-mode', choices=['legacy', 'cdi'])
        if name in {'backup', 'restore', 'update', 'rollback'}:
            command.add_argument('--backup-key', type=Path, required=True)
        if name == 'restore':
            command.add_argument('--backup', type=Path, required=True)
            command.add_argument('--confirm-instance', required=True)
        if name == 'logs':
            command.add_argument('service', choices=['api', 'worker', 'qwen', 'embedding', 'mysql', 'redis', 'qdrant', 'frontend'])
            command.add_argument('--tail', type=int, default=100)
        if name == 'verify':
            command.add_argument('--fresh', action='store_true')
        if name == 'publish':
            command.add_argument('--mode', choices=['private', 'lan', 'gateway', 'direct'], required=True)
            command.add_argument('--bind')
            command.add_argument('--port', type=int)
            command.add_argument('--public-url')
        if name == 'uninstall':
            command.add_argument('--purge', action='store_true')
            command.add_argument('--confirm-instance')
    fetch = sub.add_parser('fetch', help='download a signed version from GitHub Release or an HTTPS artifact server')
    fetch.add_argument('--version', required=True)
    fetch.add_argument('--base-url', required=True)
    fetch.add_argument('--destination', type=Path, required=True)
    fetch.add_argument('--trusted-key', type=Path, required=True)
    package = sub.add_parser('package-verify', help='verify publisher signature and every release file without installing')
    package.add_argument('--release', type=Path, required=True)
    package.add_argument('--trusted-key', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'fetch':
            from .fetch import fetch_release
            fetch_release(args.base_url, args.version, args.destination, args.trusted_key)
            return 0
        if args.command == 'package-verify':
            from .core import verify_release
            release = verify_release(args.release, args.trusted_key)
            print('VERIFIED', release['version'], release['_sha256'])
            return 0
        site = load_site(args.config)
        root = Path(site['data_dir'])
        if (root / 'config/site.json').exists():
            saved = load_site(root / 'config/site.json')
            if saved['instance'] != site['instance']:
                raise DeploymentError('configuration does not match installed instance')
            if args.command != 'install':
                site = saved
            elif args.runtime_source is None:
                site['runtime_source'] = saved.get('runtime_source', 'image')
        if args.command == 'doctor':
            print(json.dumps(doctor(site), indent=2))
        elif args.command == 'install':
            if args.gpu_access_mode:
                site['gpu_access_mode'] = args.gpu_access_mode
            if args.runtime_source:
                site['runtime_source'] = args.runtime_source
            trusted = args.trusted_key
            if trusted is None:
                trusted = root / 'config/publisher.pem'
            lifecycle.install(site, args.release, trusted, args.version, backup_key=args.backup_key)
        elif args.command == 'network-plan':
            from .networks import preflight as network_preflight
            print(json.dumps(network_preflight(site), indent=2))
        elif args.command == 'network-recover':
            lifecycle.recover_network(site)
        elif args.command in {'update', 'rollback'}:
            if args.command == 'update' and args.gpu_access_mode:
                site['gpu_access_mode'] = args.gpu_access_mode
            if args.version:
                metadata = json.loads((args.release / 'release.json').read_text())
                if metadata.get('version') != args.version:
                    raise DeploymentError('requested update version does not match package')
            lifecycle.update(site, args.release, args.trusted_key, rollback=args.command == 'rollback', backup_key=args.backup_key)
        elif args.command in {'start', 'model-start'}:
            lifecycle.start(site)
        elif args.command in {'stop', 'model-stop'}:
            lifecycle.stop(site, models_only=args.command == 'model-stop')
        elif args.command == 'verify':
            with installation_lock(root):
                lifecycle.verify(site, fresh=args.fresh)
        elif args.command in {'status', 'version'}:
            state = lifecycle.installed(site)
            output = {k: state[k] for k in ['version', 'tag', 'source', 'release_sha256', 'images', 'acceptance']}
            output['maintenance'] = (root / 'data/control/maintenance').exists()
            if args.command == 'status':
                output['containers'] = Engine(site).compose('ps', '-a', '--format', 'json')
            print(json.dumps(output, indent=2))
        elif args.command == 'logs':
            lifecycle.installed(site)
            if not 1 <= args.tail <= 5000:
                raise DeploymentError('tail must be between 1 and 5000')
            Engine(site).compose('logs', '--tail', str(args.tail), args.service, capture=False)
        elif args.command == 'backup':
            from .backup import make_backup
            make_backup(site, lifecycle.installed(site), args.backup_key)
        elif args.command == 'restore':
            from .backup import restore_backup
            restore_backup(site, lifecycle.installed(site), args.backup, args.backup_key, args.confirm_instance)
        elif args.command == 'publish':
            lifecycle.publish(site, mode=args.mode, bind=args.bind, port=args.port, public_url=args.public_url)
        elif args.command == 'uninstall':
            lifecycle.installed(site)
            if args.purge and args.confirm_instance != site['instance']:
                raise DeploymentError('purge requires matching --confirm-instance before any service is stopped')
            with installation_lock(root):
                engine = Engine(site)
                engine.drain()
                engine.compose('down', '--remove-orphans')
                if args.purge:
                    import shutil
                    if root.is_symlink() or not shutil.rmtree.avoids_symlink_attacks or any(path.is_mount() for path in root.rglob('*')):
                        raise DeploymentError('purge refused: unsafe root, nested mount, or non-hardened rmtree')
                    shutil.rmtree(root)
            print('Uninstalled project; data ' + ('removed' if args.purge else 'preserved'))
    except (DeploymentError, OSError, ValueError, KeyError) as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        print('No driver changes or global Docker prune were performed. Inspect state/operation.json and ccheck logs.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
