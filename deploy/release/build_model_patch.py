"""Publish rc17 with a new Qwen runtime and a narrowly scoped management adapter."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from repack_deployment import (
    METADATA, PATCH_FILES, REPLACED, bootstrap, read_json, reuse_file, signing_key,
    tooling_path, verify_source, write_json,
)
from build_release import export_image, license_inventory, render_install_guide
from ccheck.core import checked_path, digest, verify_release
from ccheck.image_identity import identity, inspect_image
from ccheck.models import validate_model_layout
from ccheck.inference import QWEN3

VERSION = 'v0.1.0-rc.17'
ADAPTER_INPUTS = {
    'backend/app/deployment.py', 'backend/tests/test_deployment.py',
    'deploy/release/runtime/qwen_entry.py', 'deploy/release/Dockerfile.backend-model-adapter',
    'deploy/release/build_model_patch.py', 'deploy/release/INSTALL-QWEN3.template.md',
    'deploy/release/build_online_release.py', 'deploy/release/ONLINE-INSTALL.template.md',
    'deploy/release/site-online.example.json',
}


def verify_inputs(base, source, previous_root):
    previous = verify_source(previous_root)
    if any(previous[key] != base['source'][key] for key in ('git_sha', 'tree_sha256')):
        raise RuntimeError('base source differs from the signed rc16 source')
    changed = sorted(name for name in previous['files'].keys() | source['files'].keys()
                     if previous['files'].get(name) != source['files'].get(name))
    forbidden = [name for name in changed if not tooling_path(name) and name not in ADAPTER_INPUTS]
    if forbidden:
        raise RuntimeError('rc17 cannot change review core, prompts or unrelated image inputs: ' + ', '.join(forbidden))
    return changed


def verify_models(base, manifest_path, model_root):
    old = read_json(checked_path(base['_root'], 'models/model-manifest.json'))
    manifest = read_json(manifest_path)
    rows = {row['key']: row for row in manifest['models']}
    original = {row['key']: row for row in old['models']}
    if len(rows) != len(manifest['models']) or set(rows) != {'qwen', 'jina'}:
        raise RuntimeError('expected exactly Qwen and Jina model manifests')
    if rows['jina'] != original['jina']:
        raise RuntimeError('rc17 must preserve the complete Jina revision and vector index contract')
    qwen = rows['qwen']
    if qwen['repository'] != 'Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8':
        raise RuntimeError('rc17 requires the official Qwen3-Coder FP8 checkpoint')
    names = [entry['path'] for entry in qwen['files']]
    if len(names) != len(set(names)):
        raise RuntimeError('duplicate Qwen model file')
    for entry in qwen['files']:
        path = checked_path(model_root, entry['path'])
        if not path.is_file() or path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
            raise RuntimeError('Qwen file differs from the approved manifest: ' + entry['path'])
    validate_model_layout(qwen, model_root)
    config = read_json(model_root / 'config.json')
    if (config.get('model_type') != 'qwen3_moe'
            or config['quantization_config'].get('weight_block_size') != [128, 128]):
        raise RuntimeError('expected Qwen3 MoE with official block FP8 weights')
    return manifest


def verify_report(path, source, manifest_path, images, runtime_context):
    report = read_json(path)
    if report.get('passed') is not True or report.get('source_tree') != source['tree_sha256']:
        raise RuntimeError('build gates must pass for this exact frozen source')
    for name in ('backend', 'runtime', 'models', 'tool_tests', 'security'):
        if report.get(name, {}).get('passed') is not True:
            raise RuntimeError('missing passing build gate: ' + name)
    if report['models'].get('manifest_sha256') != digest(manifest_path):
        raise RuntimeError('model evidence does not cover this manifest')
    if report['runtime'].get('lock_sha256') != digest(runtime_context / 'locks/qwen.lock'):
        raise RuntimeError('runtime evidence does not cover this dependency lock')
    for name, section in [('backend', 'backend'), ('qwen', 'runtime')]:
        if report[section].get('image_identity') != identity(inspect_image(images[name])):
            raise RuntimeError('build evidence does not cover the supplied image: ' + name)
    if report['backend'].get('compiled_core_unchanged') is not True:
        raise RuntimeError('native core parity with the original signed wheel is required')
    return report


def build(args):
    source_root, output = args.source.resolve(), args.output.resolve()
    patch = output.with_name(output.name + '-patch')
    if args.version != VERSION or output.exists() or patch.exists():
        raise RuntimeError('rc17 requires new output and patch directories')
    key = signing_key(args.signing_key, [source_root, args.base_release, output, patch,
                                      args.runtime_context, args.model_root, args.yaml_package])
    base = verify_release(args.base_release, args.trusted_key)
    if base['version'] != 'v0.1.0-rc.16':
        raise RuntimeError('rc17 must be based on the trusted rc16 release')
    source = verify_source(source_root, committed=True)
    changed = verify_inputs(base, source, args.base_source)
    manifest = verify_models(base, args.models_manifest, args.model_root)
    built = read_json(args.images)
    images = {name: built[name]['id'] for name in ('backend', 'qwen')}
    report = verify_report(args.build_report, source, args.models_manifest, images, args.runtime_context)
    if digest(args.runtime_context / 'deploy/release/runtime/qwen_entry.py') != source['files']['deploy/release/runtime/qwen_entry.py']:
        raise RuntimeError('runtime launcher differs from frozen source')
    output.mkdir(mode=0o700, parents=True)
    (output / 'images').mkdir()
    replaced = set(REPLACED) | {'models/model-manifest.json', 'models/qwen-MODEL-NOTICE.md',
                               'licenses/python-components.json', 'verify/build-gates.json'}
    for name in base['files']:
        if (name in replaced or name.startswith(('runtime/', 'locks/qwen.', 'licenses/wheels/qwen/',
                                                'images/backend.', 'images/qwen.'))):
            continue
        reuse_file(checked_path(args.base_release, name), checked_path(output, name), image=name.startswith('images/'))
    reuse_file(args.base_release / 'verify/build-gates.json', output / 'verify/rc16-build-gates.json')
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output/'publisher.pem')], check=True)
    if digest(output/'publisher.pem') != base['_publisher']:
        raise RuntimeError('publisher identity must remain unchanged')
    for name in ('ccheck', 'install.sh'):
        shutil.copy2(source_root/'deploy/release'/name, output/name)
        (output/name).chmod(0o755)
    shutil.copy2(output/'ccheck', output/'ccheckctl')
    subprocess.run(['python3', '-B', str(source_root/'deploy/release/build_cli.py'), '--output',
                    str(output/'cli/ccheck.pyz'), '--yaml-package', str(args.yaml_package)], check=True)
    example = read_json(source_root/'deploy/release/site-rtx5880.example.json')
    example['review_model'] = QWEN3
    example['model_paths']['qwen'] = '/home/lyh/models/Qwen3-Coder-30B-A3B-Instruct-FP8'
    write_json(output/'site-rtx5880.example.json', example)
    guide = render_install_guide((source_root/'deploy/release/INSTALL-QWEN3.template.md').read_text(),
                                 VERSION, digest(output/'publisher.pem'))
    (output/'INSTALL-RTX5880.md').write_text(guide)
    (output/'README.md').write_text('# C-Check '+VERSION+'\n\nOfficial Qwen3-Coder FP8 model/runtime migration. '
        'See INSTALL-RTX5880.md for prerequisites, signature verification, model prefetch, explicit migration and rollback. '
        'The native review core, prompts, Jina and five unrelated images remain unchanged. '
        'See verify/build-gates.json for actual hardware and test scope; target RTX5880 acceptance is not implied.\n')
    shutil.copy2(args.models_manifest, output/'models/model-manifest.json')
    shutil.copy2(args.model_root/'README.md', output/'models/qwen-MODEL-NOTICE.md')
    shutil.copy2(args.model_root/'LICENSE', output/'models/qwen-LICENSE')
    for path in (args.runtime_context/'locks').glob('qwen.*'):
        shutil.copy2(path, output/'locks'/path.name)
    old_inventory = read_json(args.base_release/'licenses/python-components.json')
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        notices = Path(temporary)
        license_inventory(args.runtime_context, notices)
        new_inventory = read_json(notices/'licenses/python-components.json')
        if not new_inventory or {row['environment'] for row in new_inventory} != {'qwen'}:
            raise RuntimeError('new wheel inventory must contain only the Qwen runtime')
        shutil.copytree(notices/'licenses/wheels', output/'licenses/wheels', dirs_exist_ok=True)
    write_json(output/'licenses/python-components.json', [row for row in old_inventory if row['environment']!='qwen']+new_inventory)
    shutil.copy2(args.build_report, output/'verify/build-gates.json')
    specifications = copy.deepcopy(base['images'])
    for name, image in images.items():
        metadata = inspect_image(image)
        specifications[name] = {'id': metadata['Id'], 'size_bytes': metadata['Size'],
            'filesystem_config_sha256': identity(metadata), 'archive_parts': export_image(image, output/'images', name)}
    bootstrap(output)
    files = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
    (output/'checksums.sha256').write_text(''.join(value+'  '+name+'\n' for name,value in sorted(files.items())))
    files['checksums.sha256'] = digest(output/'checksums.sha256')
    release = copy.deepcopy(base)
    for name in ('_root', '_sha256', '_publisher', 'runtime_recipe'):
        release.pop(name, None)
    reused = sorted(name for name in files if name.startswith('images/') and not name.startswith(('images/backend.', 'images/qwen.')))
    inherited = base['distribution']
    release.update(version=VERSION, tag='release-'+VERSION,
        source={key:source[key] for key in ('git_sha','tree_sha256','worktree_snapshot')},
        images=specifications, inference={'served_model':QWEN3, 'qwen_storage':'manifest-sha256'},
        files=files, artifact_sizes={name:(output/name).stat().st_size for name in files},
        bootstrap={'file':'bootstrap.tar.gz','sha256':files['bootstrap.tar.gz'],'size':(output/'bootstrap.tar.gz').stat().st_size},
        runtime_launchers={name:source['files']['deploy/release/cli/ccheck/launchers/'+name]
                           for name in ('qwen3_gpu.py','embedding_gpu.py')},
        distribution={'kind':'deployment-patch','base_version':inherited['base_version'],
            'base_release_sha256':inherited['base_release_sha256'], 'reused_files':reused,
            'upgrade_from_version':base['version'],'upgrade_from_sha256':base['_sha256']},
        artifact_reuse={'version':base['version'],'release_sha256':base['_sha256'],
            'core_wheel':'unchanged','models':{'jina':'unchanged','qwen':'replaced'},
            'images':['frontend','embedding','mysql','redis','qdrant'], 'changed_source_files':changed},
        validation={'build_gates':{'passed':True,'report':'verify/build-gates.json','source_tree':source['tree_sha256']},
            'target_rtx5880_tested':report.get('target_rtx5880_tested',False),
            'installation':'publication requires separate installation and model migration acceptance',
            'inherited_core_report':'verify/rc16-build-gates.json'})
    write_json(output/'release.json', release)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(output/'release.json'),
                    '-out',str(output/'release.sig')],check=True)
    verified = verify_release(output, args.trusted_key, base['_publisher'])
    if verify_source(source_root, committed=True) != source:
        raise RuntimeError('frozen source changed during assembly')
    patch.mkdir(mode=0o700)
    for name in PATCH_FILES:
        shutil.copy2(output/name, patch/name)
    for name in files:
        if name.startswith(('images/backend.', 'images/qwen.')):
            shutil.copy2(output/name, patch/Path(name).name)
    print('RC17_MODEL_PATCH_READY', verified['_sha256'], patch, flush=True)


def main():
    parser = argparse.ArgumentParser()
    for name in ('source','base-release','base-source','trusted-key','signing-key','models-manifest',
                 'model-root','runtime-context','images','build-report','yaml-package','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--version',default=VERSION)
    build(parser.parse_args())


if __name__ == '__main__':
    main()
