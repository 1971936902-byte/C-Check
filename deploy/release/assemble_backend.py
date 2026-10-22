import json
from pathlib import Path
import shutil

def assemble(source=Path('/src/backend'), output=Path('/artifacts/backend'),
             policy_path=Path('/policy/core-policy.json')):
    policy = json.loads(policy_path.read_text())
    core_paths = {name.replace('.', '/') + '.py' for name in policy['compiled_modules']}
    known = core_paths | set(policy['plaintext_allowlist'])
    actual = {path.relative_to(source).as_posix() for path in (source / 'app').rglob('*.py')}
    if actual != known:
        raise RuntimeError('Core policy/source mismatch: ' + str(actual.symmetric_difference(known)))
    for relative in policy['plaintext_allowlist'] + policy['resource_allowlist']:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    for name in policy['compiled_modules']:
        parent = output / Path(name.replace('.', '/')).parent
        if len(list(parent.glob(name.rsplit('.', 1)[1] + '.*.so'))) != 1:
            raise RuntimeError('Missing native artifact: ' + name)
    shutil.copytree(source / 'alembic', output / 'alembic', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(source / 'alembic.ini', output / 'alembic.ini')
    # Trusted build inputs may be owner-only; those modes must not reach runtime.
    paths = [output, *output.rglob('*')]
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
        raise RuntimeError('Unsupported runtime artifact type')
    for path in paths:
        path.chmod(0o755 if path.is_dir() else 0o644)


if __name__ == '__main__':
    assemble()
