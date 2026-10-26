"""Signed rc19 backend/CLI patch; retain rc18 model, frontend and dependency identities."""
import argparse
import copy
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from repack_deployment import REPLACED, bootstrap, read_json, reuse_file, signing_key, verify_source, write_json
from build_release import core_wheel, export_image, render_install_guide
from ccheck.core import checked_path, digest, verify_release
from ccheck.image_identity import identity, inspect_image
from ccheck.schema_migration import epoch, validate

VERSION = 'v0.1.0-rc.19'
CHANGED_CORE = {'app.services.model_router'}


def validate_sources(base, source, previous, root):
    if base['version'] != 'v0.1.0-rc.18':
        raise RuntimeError('rc19 requires the signed rc18 baseline')
    if any(previous[key] != base['source'][key] for key in ('git_sha', 'tree_sha256')):
        raise RuntimeError('base source differs from signed rc18 source')
    policy = read_json(root / 'deploy/release/core-policy.json')
    changed = {module for module in policy['compiled_modules']
               if source['files'].get('backend/' + module.replace('.', '/') + '.py')
               != previous['files'].get('backend/' + module.replace('.', '/') + '.py')}
    if changed != CHANGED_CORE:
        raise RuntimeError('unexpected native module change set: ' + str(changed))
    for name in set(source['files']) | set(previous['files']):
        frozen = (name.startswith(('frontend/', 'deploy/models/', 'deploy/release/runtime/',
                                  'deploy/release/cli/ccheck/launchers/'))
                  or name in {'backend/requirements.txt', 'deploy/release/core-policy.json',
                              'deploy/release/site-rtx5880.example.json'})
        if frozen and source['files'].get(name) != previous['files'].get(name):
            raise RuntimeError('frontend/model/dependency configuration changed: ' + name)
    return policy


def build(args):
    root, output = args.source.resolve(), args.output.resolve()
    key = signing_key(args.signing_key, [root, output, args.base_release, args.base_source])
    if output.exists():
        raise RuntimeError('output must be new')
    base = verify_release(args.base_release, args.trusted_key)
    source = verify_source(root, committed=True)
    validate_sources(base, source, verify_source(args.base_source, committed=True), root)
    report = read_json(args.build_report)
    images = read_json(args.images)
    if report.get('passed') is not True or report.get('source_tree') != source['tree_sha256']:
        raise RuntimeError('passing frozen-source gates required')
    for section in ('backend', 'frontend', 'tool_tests', 'review_cli', 'security', 'migration'):
        if report.get(section, {}).get('passed') is not True:
            raise RuntimeError('missing gate: ' + section)
    if report['backend']['image_identity'] != identity(inspect_image(images['backend'])):
        raise RuntimeError('backend gate/image mismatch')
    if not report['migration'].get('scan_progress') or report.get('base_sha256') != base['_sha256']:
        raise RuntimeError('migration/base evidence mismatch')
    output.mkdir(mode=0o700, parents=True)
    replaced = REPLACED | {'verify/build-gates.json', base['core_wheel']['file']}
    for name in base['files']:
        if name in replaced or name.startswith('images/backend.'):
            continue
        reuse_file(checked_path(args.base_release, name), checked_path(output, name), image=name.startswith('images/'))
    reuse_file(args.base_release / 'verify/build-gates.json', output / 'verify/rc18-build-gates.json')
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output/'publisher.pem')], check=True)
    if digest(output/'publisher.pem') != base['_publisher']:
        raise RuntimeError('publisher changed')
    for name in ('ccheck', 'install.sh', 'ccheck-review'):
        shutil.copy2(root/'deploy/release'/name, output/name)
        (output/name).chmod(0o755)
    shutil.copy2(output/'ccheck', output/'ccheckctl')
    subprocess.run([sys.executable, '-B', str(root/'deploy/release/build_cli.py'), '--output', str(output/'cli/ccheck.pyz'),
                    '--yaml-package', str(args.yaml_package)], check=True)
    subprocess.run([sys.executable, '-B', str(root/'tools/review-cli/build_zipapp.py'),
                    '--output', str(output/'cli/ccheck-review.pyz')], check=True)
    shutil.copy2(root/'tools/review-cli/README.md', output/'CLI-REVIEW.md')
    shutil.copy2(args.build_report, output/'verify/build-gates.json')
    shutil.copy2(args.base_release/'site-rtx5880.example.json', output/'site-rtx5880.example.json')
    guide = render_install_guide((root/'deploy/release/INSTALL-REVIEW-CLI-PATCH.template.md').read_text(encoding='utf-8'),
                                 VERSION, digest(output/'publisher.pem'))
    (output/'INSTALL-RTX5880.md').write_text(guide, encoding='utf-8')
    (output/'README.md').write_text('# C-Check '+VERSION+'\n\nSigned rc18 -> rc19 backend and review CLI patch. '
        'Models, inference configuration and frontend remain unchanged. Use INSTALL-RTX5880.md and CLI-REVIEW.md. '
        'The scan-count migration requires encrypted backup. No new target-GPU or accuracy claim.\n')
    wheel = core_wheel(root, images['backend'], output, VERSION)
    if {row['file']: row['sha256'] for row in wheel['modules']} != report['backend']['native_sha256']:
        raise RuntimeError('native wheel/gate mismatch')
    specifications = copy.deepcopy(base['images'])
    metadata = inspect_image(images['backend'])
    specifications['backend'] = {'id': metadata['Id'], 'size_bytes': metadata['Size'],
        'filesystem_config_sha256': identity(metadata), 'archive_parts': export_image(images['backend'], output/'images', 'backend')}
    schema = {p.name: digest(p) for p in (root/'backend/alembic/versions').glob('*.py')}
    release = copy.deepcopy(base)
    for name in ('_root', '_sha256', '_publisher'):
        release.pop(name, None)
    release.update(version=VERSION, tag='release-'+VERSION, schema_files=schema, schema_epoch=epoch(schema),
        schema_migration={'kind': 'additive-scan-progress', 'from_release_sha256': base['_sha256'],
            'from_epoch': base['schema_epoch'], 'to_epoch': epoch(schema),
            'migration_sha256': schema['0017_review_scan_progress.py'], 'backup_required': True})
    validate(base, release)
    bootstrap(output)
    files = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
    (output/'checksums.sha256').write_text(''.join(value+'  '+name+'\n' for name, value in sorted(files.items())))
    files['checksums.sha256'] = digest(output/'checksums.sha256')
    reused = sorted(name for name in files if name.startswith('images/') and not name.startswith('images/backend.'))
    release.update(source={key: source[key] for key in ('git_sha', 'tree_sha256', 'worktree_snapshot')},
        images=specifications, core_wheel=wheel, files=files,
        artifact_sizes={name: (output/name).stat().st_size for name in files},
        bootstrap={'file': 'bootstrap.tar.gz', 'sha256': files['bootstrap.tar.gz'], 'size': (output/'bootstrap.tar.gz').stat().st_size},
        distribution={'kind': 'deployment-patch', 'base_version': base['distribution']['base_version'],
            'base_release_sha256': base['distribution']['base_release_sha256'], 'reused_files': reused,
            'asset_versions': {name: base['version'] for name in reused if name.startswith('images/frontend.')},
            'upgrade_from_version': base['version'], 'upgrade_from_sha256': base['_sha256']},
        artifact_reuse={'version': base['version'], 'release_sha256': base['_sha256'],
                       'core_wheel': 'model_router recompiled; other native modules unchanged', 'models': 'unchanged',
                       'images': ['frontend', 'qwen', 'embedding', 'mysql', 'redis', 'qdrant']},
        review_cli={'version': '0.2.1', 'file': 'cli/ccheck-review.pyz', 'wrapper': 'ccheck-review', 'guide': 'CLI-REVIEW.md'},
        validation={'build_gates': {'passed': True, 'report': 'verify/build-gates.json', 'source_tree': source['tree_sha256']},
                    'target_rtx5880_tested': False, 'gpu_runtime': 'unchanged from rc18',
                    'installation': 'isolated source/native, MySQL migration and CLI tests; recipient acceptance required'})
    write_json(output/'release.json', release)
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in', str(output/'release.json'),
                    '-out', str(output/'release.sig')], check=True)
    verified = verify_release(output, args.trusted_key, base['_publisher'])
    if verify_source(root, committed=True) != source:
        raise RuntimeError('source changed during assembly')
    print('RC19_SIGNED_REVIEW_CLI_PATCH', verified['_sha256'], output, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('source', 'base-release', 'base-source', 'trusted-key', 'signing-key', 'images', 'build-report', 'yaml-package', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    build(parser.parse_args())
