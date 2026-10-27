"""Build the rc20 frontend patch from signed rc19; never use an A3B baseline."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from repack_deployment import METADATA, bootstrap, read_json, reuse_file, signing_key, verify_source, write_json
from build_release import export_image, render_install_guide
from ccheck.core import checked_path, digest, verify_release
from ccheck.fetch import asset_prefix
from ccheck.image_identity import identity, inspect_image, locate
from validate_app_patch import frontend_smoke
from audit_images import inspect_layers

VERSION = 'v0.1.0-rc.20'
BASE_VERSION = 'v0.1.0-rc.19'
BASE_SHA = '970091c37acdb4eaedcbca1be3e9ecf4e1cdfacc207a0b212e007f7ec427de75'
ALLOWED = {
    'frontend/src/layouts/AppLayout.vue', 'frontend/src/views/HelpView.vue',
    'frontend/public/terminal-config.json', 'frontend/src/utils/terminal-config.ts',
    'frontend/src/utils/terminal-config.test.ts', 'deploy/release/pack_source.py',
    'deploy/release/build_frontend_patch.py', 'deploy/release/INSTALL-FRONTEND-PATCH.template.md',
    'deploy/release/cli/ccheck/terminal_config.py', 'deploy/release/cli/ccheck/core.py',
    'deploy/release/cli/ccheck/engine.py', 'deploy/release/tests/test_frontend_patch.py',
    'deploy/release/cli/ccheck/__main__.py',
}


def validate_sources(base, source, previous):
    if base['version'] != BASE_VERSION or base['_sha256'] != BASE_SHA:
        raise RuntimeError('only the approved signed rc19 / Qwen2.5 baseline is supported')
    if any(previous[k] != base['source'][k] for k in ('git_sha', 'tree_sha256')):
        raise RuntimeError('baseline source does not match signed rc19')
    changed = sorted(n for n in set(previous['files']) | set(source['files'])
                     if previous['files'].get(n) != source['files'].get(n))
    forbidden = [n for n in changed if n not in ALLOWED and not n.startswith('deploy/terminal/')]
    if forbidden:
        raise RuntimeError('out-of-scope source changes: ' + ', '.join(forbidden))
    if not changed:
        raise RuntimeError('empty patch')
    return changed


def build(args):
    root, output, gates = args.source.resolve(), args.output.resolve(), args.gates.resolve()
    key = signing_key(args.signing_key, [root, output, gates, args.base_release, args.base_source])
    if output.exists() or gates.exists():
        raise RuntimeError('new output and gates directories required')
    source = verify_source(root, committed=True)
    base = verify_release(args.base_release, args.trusted_key)
    changed = validate_sources(base, source, verify_source(args.base_source, committed=True))
    models = read_json(args.base_release / 'models/model-manifest.json')
    if next(m['repository'] for m in models['models'] if m['key'] == 'qwen') != 'okwinds/Qwen2.5-Coder-32B-Instruct-FP8':
        raise RuntimeError('model baseline is not the approved Qwen2.5 FP8 model')
    old_front = locate(base['images']['frontend'])
    if not old_front:
        raise RuntimeError('signed rc19 frontend image must be loaded')
    gates.mkdir(parents=True, mode=0o700)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(root / 'deploy/release/cli'))
    def run(command, name):
        print('RUN', name, flush=True)
        with (gates / (name + '.log')).open('wb') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(root/'deploy/release/tests'), '-v'], 'tool-tests')
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(root/'deploy/terminal'), '-p', 'test_*.py', '-v'], 'terminal-tests')
    lock = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat', args.frontend_tools, '/source/package-lock.json'])
    if hashlib.sha256(lock).hexdigest() != source['files']['frontend/package-lock.json']:
        raise RuntimeError('frontend toolchain lockfile mismatch')
    context = gates / 'frontend-context'
    context.mkdir()
    run(['docker', 'run', '--rm', '--network', 'none', '--memory', '4g', '--cpus', '2', '--entrypoint', 'sh',
         '-v', str(root/'frontend')+':/input:ro', '-v', str(context)+':/output', args.frontend_tools,
         '-ec', 'mkdir /tmp/frontend; cp -a /input/. /tmp/frontend/; ln -s /source/node_modules /tmp/frontend/node_modules; '
         'cd /tmp/frontend; VITE_USE_MOCK_API=true npm test; VITE_USE_MOCK_API=false npm run build; cp -a dist /output/'], 'frontend-tests-build')
    dist = context / 'dist'
    if json.loads((dist/'terminal-config.json').read_text()) != {'url': ''}:
        raise RuntimeError('production terminal link must be disabled by default')
    image = 'c-check-frontend:0.1.0-rc.20'
    tag = 'c-check-rc20-base-frontend:' + old_front.split(':')[-1][:16]
    subprocess.run(['docker', 'tag', old_front, tag], check=True)
    run(['docker', 'build', '--network', 'none', '--provenance=false', '--sbom=false',
         '--build-arg', 'BASE_IMAGE='+tag, '--build-arg', 'RELEASE_VERSION='+VERSION,
         '--build-arg', 'SOURCE_SHA='+source['git_sha'], '-f', str(root/'deploy/release/Dockerfile.frontend-app-patch'),
         '-t', image, str(context)], 'frontend-image')
    frontend_smoke(image, dist)
    policy = read_json(root/'deploy/release/core-policy.json')
    audit = inspect_layers(image, policy, {source['files']['backend/'+m.replace('.', '/')+'.py'] for m in policy['compiled_modules']})
    report = {'passed': True, 'source_tree': source['tree_sha256'], 'git_sha': source['git_sha'],
              'base_sha256': base['_sha256'], 'changed_sources': changed,
              'frontend': {'passed': True, 'production_api': True, 'readonly_nonroot_http': True,
                           'image_identity': identity(inspect_image(image)), 'toolchain_image': inspect_image(args.frontend_tools)['Id']},
              'tool_tests': {'passed': True}, 'terminal_tests': {'passed': True}, 'security': {'passed': True, 'audit': audit},
              'backend': {'unchanged': True}, 'schema': {'unchanged': True}, 'models': {'unchanged': True},
              'gpu_tested': False, 'target_acceptance_required': True,
              'terminal_live_test': 'Separate CPU-server verification is documented in terminal/VERIFICATION.md; not repeated on this builder.'}
    write_json(gates/'build-gates.json', report)
    output.mkdir(parents=True, mode=0o700)
    replaced = METADATA | {'README.md', 'INSTALL-RTX5880.md', 'cli/ccheck.pyz', 'verify/build-gates.json'}
    for name in base['files']:
        if name not in replaced and not name.startswith('images/frontend.'):
            reuse_file(checked_path(args.base_release, name), checked_path(output, name), image=name.startswith('images/'))
    reuse_file(args.base_release/'verify/build-gates.json', output/'verify/rc19-build-gates.json')
    public = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'])
    if hashlib.sha256(public).hexdigest() != base['_publisher']:
        raise RuntimeError('original publisher identity required')
    run([sys.executable, '-B', str(root/'deploy/release/build_cli.py'), '--output', str(output/'cli/ccheck.pyz'),
         '--yaml-package', str(args.yaml_package)], 'build-cli')
    for path in (root/'deploy/terminal').rglob('*'):
        if path.is_file() and path.relative_to(root).as_posix() in source['files']:
            reuse_file(path, output/'terminal'/path.relative_to(root/'deploy/terminal'))
    shutil.copy2(gates/'build-gates.json', output/'verify/build-gates.json')
    guide = render_install_guide((root/'deploy/release/INSTALL-FRONTEND-PATCH.template.md').read_text(encoding='utf-8'), VERSION, base['_publisher'])
    (output/'INSTALL-RTX5880.md').write_text(guide, encoding='utf-8')
    (output/'README.md').write_text('# C-Check '+VERSION+'\n\nSigned rc19 frontend/help and optional terminal patch. '
        'Qwen2.5 FP8, Jina, backend, database schema, inference settings and review CLI are unchanged. '
        'Not an A3B release. See INSTALL-RTX5880.md. Guacamole is opt-in and downloaded separately.\n', encoding='utf-8')
    specifications = copy.deepcopy(base['images'])
    meta = inspect_image(image)
    specifications['frontend'] = {'id': meta['Id'], 'size_bytes': meta['Size'],
        'filesystem_config_sha256': identity(meta), 'archive_parts': export_image(image, output/'images', 'frontend')}
    bootstrap(output)
    files = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
    (output/'checksums.sha256').write_text(''.join(v+'  '+n+'\n' for n,v in sorted(files.items())))
    files['checksums.sha256'] = digest(output/'checksums.sha256')
    reused = sorted(n for n in files if n.startswith('images/') and not n.startswith('images/frontend.'))
    origins = {n: asset_prefix(base, n, 'https://example.invalid').rstrip('/').rsplit('/release-', 1)[1] for n in reused}
    release = {k: copy.deepcopy(v) for k,v in base.items() if not k.startswith('_')}
    release.pop('schema_migration', None)
    release.update(version=VERSION, tag='release-'+VERSION,
        source={k: source[k] for k in ('git_sha','tree_sha256','worktree_snapshot')}, images=specifications,
        files=files, artifact_sizes={n:(output/n).stat().st_size for n in files},
        bootstrap={'file':'bootstrap.tar.gz','sha256':files['bootstrap.tar.gz'],'size':(output/'bootstrap.tar.gz').stat().st_size},
        distribution={'kind':'deployment-patch','base_version':base['distribution']['base_version'],
            'base_release_sha256':base['distribution']['base_release_sha256'],'reused_files':reused,'asset_versions':origins,
            'upgrade_from_version':BASE_VERSION,'upgrade_from_sha256':base['_sha256']},
        artifact_reuse={'version':BASE_VERSION,'release_sha256':base['_sha256'],'core_wheel':'unchanged','models':'unchanged',
                       'images':['backend','qwen','embedding','mysql','redis','qdrant']},
        validation={'build_gates':{'passed':True,'report':'verify/build-gates.json','source_tree':source['tree_sha256']},
                    'target_rtx5880_tested':False,'gpu_runtime':'unchanged from signed rc19','installation':'recipient acceptance required'})
    write_json(output/'release.json', release)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(output/'release.json'),'-out',str(output/'release.sig')], check=True)
    verified = verify_release(output, args.trusted_key, base['_publisher'])
    if verify_source(root, committed=True) != source:
        raise RuntimeError('source changed during build')
    bundle = output.parent / ('release-'+VERSION+'-patch.tar.gz')
    with bundle.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in sorted((set(files) - set(reused)) | {'release.json','release.sig'}):
            archive.add(output/name, arcname=output.name+'/'+name, recursive=False)
    bundle.with_name(bundle.name+'.sha256').write_text(digest(bundle)+'  '+bundle.name+'\n')
    print('RC20_SIGNED_FRONTEND_PATCH', verified['_sha256'], str(bundle), bundle.stat().st_size, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('source','base-source','base-release','trusted-key','signing-key','yaml-package','output','gates'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--frontend-tools', required=True)
    build(parser.parse_args())
