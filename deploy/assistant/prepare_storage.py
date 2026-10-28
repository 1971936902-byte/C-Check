"""Prepare assistant-only storage and credentials without replacing existing data."""
import argparse
import os
from pathlib import Path
import secrets
import stat


KEYS = ('assistant_gateway_key', 'assistant_control_key', 'assistant_anythingllm_api_key')


def checked_child(parent, name):
    path = parent / name
    if path.is_symlink():
        raise ValueError(f'refusing symbolic link: {path}')
    return path


def directory(parent, name, uid, gid, *, private=False):
    path = checked_child(parent, name)
    if not path.exists():
        path.mkdir(mode=0o700)
        os.chown(path, uid, gid)
    if not path.is_dir():
        raise ValueError(f'expected directory: {path}')
    metadata = path.stat()
    if private and (metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) & 0o077
                    or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700):
        raise ValueError(f'unsafe owner or permissions for assistant directory: {path.name}')
    return path


def create_file(path, content, uid, gid, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'expected regular file: {path}') from None
        return False
    with os.fdopen(descriptor, 'wb') as file:
        os.fchown(file.fileno(), uid, gid)
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    return True


def prepare(root, uid=10001, gid=10001):
    if uid <= 0 or gid <= 0:
        raise ValueError('assistant containers must use a non-root identity')
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('data root must be an existing absolute directory, not a symlink')
    if root.resolve() != root:
        raise ValueError('data root must not traverse symbolic links or parent segments')
    data = directory(root, 'data', 0, 0)
    assistant = directory(data, 'assistant', uid, gid, private=True)
    for name in ('storage', 'collector-hotdir', 'collector-storage'):
        directory(assistant, name, uid, gid, private=True)
    server_env = checked_child(assistant, 'server.env')
    create_file(server_env, b'', uid, gid, 0o600)
    metadata = server_env.stat()
    if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError('unsafe owner or permissions for assistant server environment')
    secret_dir = directory(root, 'secrets', 0, 0)
    for name in KEYS:
        path = checked_child(secret_dir, name)
        create_file(path, (secrets.token_hex(32) + '\n').encode(), uid, gid, 0o400)
        metadata = path.stat()
        if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) & 0o077 or not metadata.st_mode & 0o400:
            raise ValueError(f'unsafe owner or permissions for {name}; existing file was preserved')
        if metadata.st_size > 4096:
            raise ValueError(f'invalid credential size for {name}')
        value = path.read_bytes().rstrip(b'\r\n')
        if len(value) < 32 or b'\0' in value:
            raise ValueError(f'invalid credential for {name}; existing file was preserved')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--uid', type=int, default=10001)
    parser.add_argument('--gid', type=int, default=10001)
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        parser.error('run as root on the Linux deployment host')
    prepare(args.data_dir, args.uid, args.gid)
    print('Assistant storage prepared. Existing files and credentials were preserved.')


if __name__ == '__main__':
    main()
