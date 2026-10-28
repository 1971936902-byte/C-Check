"""Filesystem snapshot helper. Installer stops writers before invoking this."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

MEMBERS = ('data/mysql', 'data/redis', 'data/qdrant', 'data/uploads', 'secrets', 'data/assistant')
OPTIONAL = {'data/assistant'}
IGNORED_LINKS = {'data/mysql/mysql.sock', 'data/mysql/mysqlx.sock'}


def permitted(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name and
            any(name == prefix or name.startswith(prefix + '/') for prefix in MEMBERS))


def create(root):
    def clean(member):
        source = root / member.name
        if source.is_symlink():
            if member.name in IGNORED_LINKS:
                return None
            raise RuntimeError('unsupported symlink in snapshot source')
        if member.islnk() and source.is_file():
            member.type, member.size, member.linkname = tarfile.REGTYPE, source.stat().st_size, ''
        if not permitted(member.name) or not (member.isfile() or member.isdir()):
            return None
        member.mode &= 0o777
        return member
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for name in MEMBERS:
            if name in OPTIONAL and not (root / name).exists() and not (root / name).is_symlink():
                continue
            if (root / name).is_symlink():
                raise RuntimeError('symlinked snapshot root refused')
            archive.add(root / name, arcname=name, filter=clean)


def restore(root, uid, gid):
    if uid < 1000 or gid < 1000:
        raise RuntimeError('restore application UID/GID must be non-root')
    for name in MEMBERS:
        path = root / name
        if path.is_symlink():
            raise RuntimeError('refusing symlinked restore target')
        if path.exists():
            shutil.rmtree(path)
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
        for member in archive:
            if not permitted(member.name) or not (member.isfile() or member.isdir()):
                raise RuntimeError('unsafe backup archive entry')
            target = root / member.name
            if any(parent.is_symlink() for parent in [target, *target.parents] if parent != root):
                raise RuntimeError('symlink in restore path')
            member.mode &= 0o777
            archive.extract(member, root, filter='fully_trusted')
    for name in ('secrets', 'data/uploads', 'data/qdrant', 'data/assistant'):
        base = root / name
        if name in OPTIONAL and not base.exists():
            continue
        for path in [base, *base.rglob('*')]:
            os.chown(path, uid, gid, follow_symlinks=False)


def measure(root):
    size, entries = 0, 0
    for name in MEMBERS:
        base = root / name
        if name in OPTIONAL and not base.exists() and not base.is_symlink():
            continue
        for path in [base, *base.rglob('*')]:
            if path.is_symlink():
                if path.relative_to(root).as_posix() in IGNORED_LINKS:
                    continue
                raise RuntimeError('unsupported symlink in snapshot source')
            if path.is_file():
                size += path.stat().st_size
            entries += 1
    print(json.dumps({'bytes': size, 'entries': entries}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['create', 'restore', 'measure'])
    parser.add_argument('--root', type=Path, default=Path('/bundle'))
    parser.add_argument('--uid', type=int, default=10001)
    parser.add_argument('--gid', type=int, default=10001)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    {'create': lambda: create(root), 'restore': lambda: restore(root, args.uid, args.gid),
     'measure': lambda: measure(root)}[args.command]()


if __name__ == '__main__':
    main()
