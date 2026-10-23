"""CPU-only deployment tooling repack; never builds or tests runtime images."""
import argparse
import copy
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True

from build_release import render_install_guide
from pack_source import EXCLUDED, PREFIXES
from ccheck.core import checked_path, digest, verify_release


METADATA = {'bootstrap.tar.gz', 'checksums.sha256', 'release.json', 'release.sig'}
PATCH_FILES = ('bootstrap.tar.gz', 'release.json', 'release.sig', 'checksums.sha256',
               'publisher.pem', 'INSTALL-RTX5880.md', 'site-rtx5880.example.json')
REPLACED = METADATA | {'publisher.pem', 'INSTALL-RTX5880.md', 'README.md',
                       'site-rtx5880.example.json', 'cli/ccheck.pyz', 'ccheck', 'ccheckctl',
                       'install.sh', 'verify/deployment-tooling.json'}
TOOL_FILES = {'build_cli.py', 'repack_deployment.py', 'ccheck', 'install.sh',
              'site-rtx5880.example.json', 'INSTALL-RTX5880.md', 'INSTALL-RTX5880.template.md',
              'README.md', 'PUBLISHER-LINUX.md'}


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def tree_hash(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def source_files(root):
    """Use pack_source's inventory rules, including detection of added inputs."""
    files = {}
    for prefix in PREFIXES:
        directory = checked_path(root, prefix)
        for path in sorted(directory.rglob('*')):
            relative = path.relative_to(root)
            if set(relative.parts) & EXCLUDED or path.suffix in {'.pyc', '.pyd'}:
                continue
            checked_path(root, relative.as_posix())
            if path.is_file():
                files[relative.as_posix()] = digest(path)
            elif not path.is_dir():
                raise RuntimeError('special file in source: ' + str(relative))
    return files


def verify_source(root, *, committed=False):
    root = Path(root).resolve()
    source = read_json(checked_path(root, 'build-source.json'))
    if not re.fullmatch(r'[0-9a-f]{40}', source.get('git_sha', '')):
        raise RuntimeError('source must identify a Git commit')
    if committed and source.get('worktree_snapshot') is not False:
        raise RuntimeError('new source must be committed, not a worktree snapshot')
    files = source.get('files')
    if not isinstance(files, dict) or not files:
        raise RuntimeError('missing source files')
    for name, checksum in files.items():
        path = checked_path(root, name)
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise RuntimeError('invalid source hash: ' + name)
        if not path.is_file() or digest(path) != checksum:
            raise RuntimeError('source checksum mismatch: ' + name)
    if source_files(root) != files or tree_hash(files) != source.get('tree_sha256'):
        raise RuntimeError('source inventory/tree_sha256 mismatch')
    if committed:
        actual = set()
        for path in root.rglob('*'):
            name = path.relative_to(root).as_posix()
            checked_path(root, name)
            if path.is_file():
                actual.add(name)
            elif not path.is_dir():
                raise RuntimeError('special file in frozen source: ' + name)
        if actual != set(files) | {'build-source.json'}:
            raise RuntimeError('unlisted file in new frozen source context')
    return source


def tooling_path(name):
    path = PurePosixPath(name)
    if not name.startswith('deploy/release/'):
        return False
    parts = path.parts[2:]
    # Even tests/docs cannot hide a newly introduced image recipe.
    if any('dockerfile' in part.lower() for part in parts):
        return False
    return bool(parts) and (parts[0] in {'cli', 'tests'} or '/'.join(parts) in TOOL_FILES)


def git_source(root, commit):
    """Fallback when the original frozen source is unavailable; read committed blobs only."""
    from pack_source import git
    # pack_source.git uses cwd; use explicit -C for callers outside the repository.
    def command(*args):
        return git('-C', str(root), *args)
    names = command('ls-tree', '-r', '--name-only', '-z', commit, '--', *PREFIXES).decode().split('\0')
    files = {}
    for name in filter(None, names):
        path = PurePosixPath(name)
        if set(path.parts) & EXCLUDED or path.suffix in {'.pyc', '.pyd'}:
            continue
        files[name] = hashlib.sha256(command('show', commit + ':' + name)).hexdigest()
    return {'git_sha': commit, 'files': files, 'tree_sha256': tree_hash(files)}


def approved_sources(base, source, base_root=None, repository=None):
    approved = base['source']
    original = verify_source(base_root) if base_root else git_source(repository, approved['git_sha'])
    for field in ('git_sha', 'tree_sha256'):
        if original[field] != approved[field]:
            raise RuntimeError('base source does not match signed base.source.' + field)
    changed = sorted(name for name in original['files'].keys() | source['files'].keys()
                     if original['files'].get(name) != source['files'].get(name))
    forbidden = [name for name in changed if not tooling_path(name)]
    if forbidden:
        raise RuntimeError('non-tooling/image input changed: ' + ', '.join(forbidden))
    return changed


def signing_key(path, roots):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('signing key must be a regular external file')
    path = path.resolve()
    if any(path.is_relative_to(root.resolve()) for root in roots):
        raise RuntimeError('signing key must be outside source, base release, output and patch')
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError('signing key must have mode 0600')
    return path


def reuse_file(source, target, *, image=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    if image:
        try:
            os.link(source, target)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    shutil.copy2(source, target)


def bootstrap(root):
    paths = [path for path in sorted(root.rglob('*')) if path.is_file()
             and path.relative_to(root).parts[0] != 'images'
             and path.relative_to(root).as_posix() not in METADATA]
    with tarfile.open(root / 'bootstrap.tar.gz', 'w:gz') as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)


def validation_report(path, source):
    report = read_json(path)
    if (report.get('passed') is not True or not isinstance(report.get('tool_tests'), dict)
            or report['tool_tests'].get('passed') is not True
            or not isinstance(report.get('network_integration'), dict)
            or report['network_integration'].get('passed') is not True
            or report.get('source_tree') != source['tree_sha256']):
        raise RuntimeError('validation report requires passed=true, tool_tests.passed=true, '
                           'network_integration.passed=true and matching source_tree')
    return report


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def repack(args):
    base_root, root, output = args.base_release.resolve(), args.source.resolve(), args.output.resolve()
    patch = output.with_name(output.name + '-patch')
    if output.exists() or patch.exists():
        raise RuntimeError('release output and sibling -patch directory must be new')
    inputs = [root, base_root] + ([args.base_source.resolve()] if args.base_source else [])
    if any(destination.is_relative_to(source) for destination in (output, patch) for source in inputs):
        raise RuntimeError('outputs must be outside source and base release')
    key = signing_key(args.signing_key, inputs + [args.yaml_package, output, patch])
    if args.version != 'v0.1.0-rc.15':
        raise RuntimeError('this repacker requires version v0.1.0-rc.15')
    base = verify_release(base_root, args.trusted_key)
    if base['version'] != 'v0.1.0-rc.14':
        raise RuntimeError('base release must be v0.1.0-rc.14')
    # Parse again with duplicate-key rejection after cryptographic verification.
    read_json(base_root / 'release.json')
    source = verify_source(root, committed=True)
    changed = approved_sources(base, source, args.base_source, Path(__file__).resolve().parents[2])
    validation_report(args.validation_report, source)
    if 'verify/build-gates.json' not in base['files']:
        raise RuntimeError('signed baseline build gates are required')
    for name in ('ccheck', 'install.sh', 'build_cli.py', 'INSTALL-RTX5880.template.md',
                 'site-rtx5880.example.json', 'cli/ccheck/__main__.py'):
        if 'deploy/release/' + name not in source['files']:
            raise RuntimeError('missing frozen deployment input: ' + name)
    if not (args.yaml_package / '__init__.py').is_file():
        raise RuntimeError('supply the validated PyYAML package directory')
    output.mkdir(mode=0o700, parents=True)
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output / 'publisher.pem')], check=True)
    if digest(output / 'publisher.pem') != base['_publisher']:
        raise RuntimeError('publisher identity must match the independently trusted base publisher')
    for name in base['files']:
        if name not in REPLACED:
            reuse_file(checked_path(base_root, name), checked_path(output, name), image=name.startswith('images/'))
    for name in ('ccheck', 'install.sh', 'site-rtx5880.example.json'):
        shutil.copy2(root / 'deploy/release' / name, output / name)
    shutil.copy2(output / 'ccheck', output / 'ccheckctl')
    for name in ('ccheck', 'ccheckctl', 'install.sh'):
        (output / name).chmod(0o755)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable, str(root / 'deploy/release/build_cli.py'), '--output',
                    str(output / 'cli/ccheck.pyz'), '--yaml-package', str(args.yaml_package.resolve())],
                   check=True, env=environment)
    template = (root / 'deploy/release/INSTALL-RTX5880.template.md').read_text(encoding='utf-8')
    (output / 'INSTALL-RTX5880.md').write_text(
        render_install_guide(template, args.version, digest(output / 'publisher.pem')), encoding='utf-8')
    (output / 'README.md').write_text(
        '# C-Check ' + args.version + '\n\nDeployment tooling update for Linux/amd64. '
        'Requires Python 3.10+, OpenSSL 3, Docker Compose and NVIDIA GPU container support. '
        'Verify publisher.pem against an independent trust anchor before installation. '
        'See INSTALL-RTX5880.md and models/ notices. '
        'Images, models and core wheel are reused unchanged from ' + base['version'] + '. '
        'Only CPU deployment tooling tests were run for this repack. '
        'verify/build-gates.json is historical baseline evidence; no new GPU acceptance was performed.\n',
        encoding='utf-8')
    (output / 'verify').mkdir(exist_ok=True)
    shutil.copy2(args.validation_report, output / 'verify/deployment-tooling.json')
    validation_report(output / 'verify/deployment-tooling.json', source)
    # Recheck frozen inputs after invoking their packager.
    if verify_source(root, committed=True) != source:
        raise RuntimeError('source changed during repack')
    bootstrap(output)
    files = {path.relative_to(output).as_posix(): digest(path)
             for path in sorted(output.rglob('*')) if path.is_file()}
    (output / 'checksums.sha256').write_text(
        ''.join(checksum + '  ' + name + '\n' for name, checksum in sorted(files.items())), encoding='utf-8')
    files['checksums.sha256'] = digest(output / 'checksums.sha256')
    release = copy.deepcopy(base)
    for name in ('_root', '_sha256', '_publisher'):
        release.pop(name, None)
    release.update(version=args.version, tag='release-' + args.version,
                   source={name: source[name] for name in ('git_sha', 'tree_sha256', 'worktree_snapshot')},
                   files=files, artifact_sizes={name: (output / name).stat().st_size for name in files},
                   bootstrap={'file': 'bootstrap.tar.gz', 'sha256': files['bootstrap.tar.gz'],
                              'size': (output / 'bootstrap.tar.gz').stat().st_size})
    release['distribution'] = {
        'kind': 'deployment-patch', 'base_version': base['version'],
        'base_release_sha256': base['_sha256'],
        'reused_files': sorted(name for name in files if name.startswith('images/'))}
    release['artifact_reuse'] = {
        'release_sha256': base['_sha256'], 'version': base['version'], 'source': base['source'],
        'images': sorted(base['images']), 'core_wheel': 'unchanged', 'models': 'unchanged',
        'reason': 'CPU-only deployment tooling repack; all runtime artifacts reused unchanged',
        'changed_source_files': changed, 'previous': base.get('artifact_reuse')}
    release['validation'] = {
        'deployment_tooling': {'report': 'verify/deployment-tooling.json', 'passed': True,
                               'source_tree': source['tree_sha256'],
                               'scope': 'CPU-only tooling and network integration tests'},
        'gpu': {'tested_this_release': False, 'inherited_from': base['version'],
                'release_sha256': base['_sha256'], 'report': 'verify/build-gates.json',
                'note': 'Historical baseline GPU evidence, not testing of this repack'},
        'inherited_metadata': base.get('validation')}
    write_json(output / 'release.json', release)
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in',
                    str(output / 'release.json'), '-out', str(output / 'release.sig')], check=True)
    verify_release(output, output / 'publisher.pem')
    for name, checksum in base['files'].items():
        if name not in REPLACED and files.get(name) != checksum:
            raise RuntimeError('reused artifact changed: ' + name)
    # Verify the original again to catch accidental writes through shared image inodes.
    verify_release(base_root, args.trusted_key)
    patch.mkdir(mode=0o700)
    for name in PATCH_FILES:
        shutil.copy2(output / name, patch / name)
    print('SIGNED_DEPLOYMENT_REPACK', output, 'PATCH', patch)
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base-release', 'trusted-key', 'source', 'output', 'signing-key', 'yaml-package', 'validation-report'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--base-source', type=Path, help='original rc14 frozen context; otherwise use local Git objects')
    parser.add_argument('--version', required=True)
    parser.epilog = ('Validation JSON: {"passed": true, "tool_tests": {"passed": true}, '
                     '"network_integration": {"passed": true}, '
                     '"source_tree": "<new build-source.json tree_sha256>"}. '
                     'Creates --output and its sibling <output>-patch; no Docker operations.')
    repack(parser.parse_args(argv))


if __name__ == '__main__':
    main()
