"""Signed rc18 application-only patch over rc16; no model/runtime replacement."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess

from repack_deployment import (METADATA, PATCH_FILES, REPLACED, bootstrap, read_json, reuse_file,
                               signing_key, verify_source, write_json)
from build_release import export_image, render_install_guide
from ccheck.core import checked_path, digest, verify_release
from ccheck.image_identity import identity, inspect_image
from ccheck.schema_migration import epoch, validate

VERSION = 'v0.1.0-rc.18'


def validate_sources(base, source, previous, root):
    if any(previous[key] != base['source'][key] for key in ('git_sha', 'tree_sha256')):
        raise RuntimeError('base source is not the signed rc16 source')
    policy = read_json(root / 'deploy/release/core-policy.json')
    for module in policy['compiled_modules']:
        name = 'backend/' + module.replace('.', '/') + '.py'
        if source['files'].get(name) != previous['files'].get(name):
            raise RuntimeError('compiled core changed; full trusted compilation is required: ' + name)
    for name in ('backend/requirements.txt', 'frontend/package.json', 'frontend/package-lock.json',
                 'deploy/release/runtime/embedding_entry.py',
                 'deploy/release/cli/ccheck/launchers/qwen_gpu.py',
                 'deploy/release/cli/ccheck/launchers/embedding_gpu.py'):
        if source['files'].get(name) != previous['files'].get(name):
            raise RuntimeError('dependency/model runtime input changed: ' + name)
    return policy


def build(args):
    root, output = args.source.resolve(), args.output.resolve()
    key = signing_key(args.signing_key, [root, output, args.base_release, args.base_source])
    if output.exists():
        raise RuntimeError('new output directory is required')
    base = verify_release(args.base_release, args.trusted_key)
    if base['version'] != 'v0.1.0-rc.16':
        raise RuntimeError('application patch requires rc16; Qwen3 rc17 is not a valid base')
    source = verify_source(root, committed=True)
    previous = verify_source(args.base_source, committed=True)
    validate_sources(base, source, previous, root)
    report = read_json(args.build_report)
    images = read_json(args.images)
    if report.get('passed') is not True or report.get('source_tree') != source['tree_sha256']:
        raise RuntimeError('passing evidence for frozen source is required')
    for section in ('backend', 'frontend', 'tool_tests', 'security', 'migration'):
        if report.get(section, {}).get('passed') is not True:
            raise RuntimeError('missing gate: ' + section)
    if report['backend'].get('compiled_core_unchanged') is not True:
        raise RuntimeError('native artifact parity is required')
    for name in ('backend', 'frontend'):
        if report[name]['image_identity'] != identity(inspect_image(images[name])):
            raise RuntimeError('gate image mismatch: ' + name)
    output.mkdir(mode=0o700, parents=True)
    replaced = REPLACED | {'core-policy.json', 'verify/build-gates.json'}
    for name in base['files']:
        if name in replaced or name.startswith(('images/backend.', 'images/frontend.')):
            continue
        reuse_file(checked_path(args.base_release, name), checked_path(output, name), image=name.startswith('images/'))
    reuse_file(args.base_release / 'verify/build-gates.json', output / 'verify/rc16-build-gates.json')
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output/'publisher.pem')], check=True)
    if digest(output/'publisher.pem') != base['_publisher']:
        raise RuntimeError('publisher changed')
    for name in ('ccheck', 'install.sh'):
        shutil.copy2(root/'deploy/release'/name, output/name)
        (output/name).chmod(0o755)
    shutil.copy2(output/'ccheck', output/'ccheckctl')
    subprocess.run(['python3', '-B', str(root/'deploy/release/build_cli.py'), '--output', str(output/'cli/ccheck.pyz'),
                    '--yaml-package', str(args.yaml_package)], check=True)
    shutil.copy2(root/'deploy/release/core-policy.json', output/'core-policy.json')
    shutil.copy2(args.build_report, output/'verify/build-gates.json')
    shutil.copy2(args.base_release/'site-rtx5880.example.json', output/'site-rtx5880.example.json')
    guide = render_install_guide((root/'deploy/release/INSTALL-APP-PATCH.template.md').read_text(encoding='utf-8'),
                                 VERSION, digest(output/'publisher.pem'))
    (output/'INSTALL-RTX5880.md').write_text(guide, encoding='utf-8')
    (output/'README.md').write_text('# C-Check '+VERSION+'\n\nApplication-only update over rc16. '
        'Qwen2.5/Jina weights, runtime images and locked dependencies are unchanged. '
        'Requires an encrypted backup and the approved additive graph-state migration. '
        'See INSTALL-RTX5880.md and verify/build-gates.json. No new target-GPU or accuracy claim.\n')
    specifications = copy.deepcopy(base['images'])
    for name in ('backend', 'frontend'):
        metadata = inspect_image(images[name])
        specifications[name] = {'id':metadata['Id'], 'size_bytes':metadata['Size'],
            'filesystem_config_sha256':identity(metadata), 'archive_parts':export_image(images[name], output/'images', name)}
    schema = {p.name:digest(p) for p in (root/'backend/alembic/versions').glob('*.py')}
    release = copy.deepcopy(base)
    for name in ('_root', '_sha256', '_publisher'):
        release.pop(name, None)
    release.update(version=VERSION, tag='release-'+VERSION, schema_files=schema, schema_epoch=epoch(schema),
                   schema_migration={'kind':'additive-graph-state','from_release_sha256':base['_sha256'],
                     'from_epoch':base['schema_epoch'],'to_epoch':epoch(schema),
                     'migration_sha256':schema['0016_manual_graph_build.py'],'backup_required':True})
    validate(base, release)
    bootstrap(output)
    files = {p.relative_to(output).as_posix():digest(p) for p in output.rglob('*') if p.is_file()}
    (output/'checksums.sha256').write_text(''.join(value+'  '+name+'\n' for name,value in sorted(files.items())))
    files['checksums.sha256'] = digest(output/'checksums.sha256')
    reused = sorted(name for name in files if name.startswith('images/') and not name.startswith(('images/backend.', 'images/frontend.')))
    release.update(source={key:source[key] for key in ('git_sha','tree_sha256','worktree_snapshot')}, images=specifications,
        files=files, artifact_sizes={name:(output/name).stat().st_size for name in files},
        bootstrap={'file':'bootstrap.tar.gz','sha256':files['bootstrap.tar.gz'],'size':(output/'bootstrap.tar.gz').stat().st_size},
        distribution={'kind':'deployment-patch','base_version':base['distribution']['base_version'],
            'base_release_sha256':base['distribution']['base_release_sha256'],'reused_files':reused,
            'upgrade_from_version':base['version'],'upgrade_from_sha256':base['_sha256']},
        artifact_reuse={'version':base['version'],'release_sha256':base['_sha256'],
                       'core_wheel':'unchanged','models':'unchanged',
                       'images':['qwen','embedding','mysql','redis','qdrant']},
        validation={'build_gates':{'passed':True,'report':'verify/build-gates.json','source_tree':source['tree_sha256']},
                    'target_rtx5880_tested':False,'gpu_runtime':'unchanged from rc16',
                    'installation':'isolated migration and application tests; recipient acceptance remains required'})
    write_json(output/'release.json', release)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(output/'release.json'),
                    '-out',str(output/'release.sig')],check=True)
    verified = verify_release(output,args.trusted_key,base['_publisher'])
    if verify_source(root, committed=True) != source:
        raise RuntimeError('source changed during assembly')
    print('RC18_SIGNED_APPLICATION_PATCH', verified['_sha256'], output, flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','base-release','base-source','trusted-key','signing-key','images','build-report','yaml-package','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    build(parser.parse_args())
