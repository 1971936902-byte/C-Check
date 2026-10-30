"""Publish a narrowly scoped, CPU-built assistant UI patch from signed rc22."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / 'cli'))
from repack_deployment import METADATA, bootstrap, reuse_file, signing_key, verify_source
from build_release import export_image, patch_bundle, render_install_guide
from ccheck.core import digest, verify_release
from ccheck.fetch import asset_prefix
from ccheck.image_identity import identity, inspect_image, locate
from validate_app_patch import frontend_smoke
from audit_images import inspect_layers

VERSION = 'v0.1.0-rc.23'
BASE_SHA = 'ab6be15387f4428fc2c34aa9cd98dfefcf3b49a661bc25419b5d81e33c4db9f5'
ALLOWED = {
    'frontend/package.json', 'frontend/package-lock.json', 'frontend/src/api/assistant.ts',
    'frontend/src/views/AssistantView.vue', 'frontend/src/components/AssistantMarkdown.vue',
    'frontend/src/components/assistant-markdown.ts', 'frontend/src/components/assistant-markdown.test.ts',
    'frontend/src/views/assistant-interactions.ts', 'frontend/src/views/assistant-interactions.test.ts',
    'deploy/release/build_assistant_ui_patch.py',
    'deploy/release/INSTALL-ASSISTANT-UI-PATCH.template.md',
    'deploy/release/tests/test_assistant_ui_patch.py',
}


def validate_sources(base, source, previous):
    if base['version'] != 'v0.1.0-rc.22' or base['_sha256'] != BASE_SHA:
        raise RuntimeError('only the original signed rc22 baseline is supported')
    if any(previous[key] != base['source'][key] for key in ('git_sha', 'tree_sha256')):
        raise RuntimeError('baseline source differs from signed rc22')
    changed = sorted(name for name in set(previous['files']) | set(source['files'])
                     if previous['files'].get(name) != source['files'].get(name))
    if not changed or set(changed) - ALLOWED:
        raise RuntimeError('empty or out-of-scope assistant UI patch')
    return changed


def package_metadata(base, source, output, frontend):
    release = {key: copy.deepcopy(value) for key, value in base.items() if not key.startswith('_')}
    release.pop('schema_migration', None)
    release.update(version=VERSION, tag='release-' + VERSION,
                   source={key: source[key] for key in ('git_sha', 'tree_sha256', 'worktree_snapshot')})
    release['images']['frontend'] = frontend
    files = {path.relative_to(output).as_posix(): digest(path) for path in output.rglob('*') if path.is_file()}
    (output / 'checksums.sha256').write_text(''.join(value + '  ' + name + '\n' for name, value in sorted(files.items())))
    files['checksums.sha256'] = digest(output / 'checksums.sha256')
    release.update(files=files, artifact_sizes={name: (output / name).stat().st_size for name in files},
                   bootstrap={'file': 'bootstrap.tar.gz', 'sha256': files['bootstrap.tar.gz'],
                              'size': (output / 'bootstrap.tar.gz').stat().st_size})
    reused = sorted(name for name in files if name.startswith('images/') and not name.startswith('images/frontend.'))
    release['distribution'] = {
        'kind': 'deployment-patch', 'base_version': base['distribution']['base_version'],
        'base_release_sha256': base['distribution']['base_release_sha256'],
        'upgrade_from_version': base['version'], 'upgrade_from_sha256': base['_sha256'],
        'reused_files': reused, 'asset_versions': {
            name: asset_prefix(base, name, 'https://example.invalid').rstrip('/').rsplit('/release-', 1)[1]
            for name in reused}}
    release['artifact_reuse'] = {'version': base['version'], 'release_sha256': base['_sha256'],
        'core_wheel': 'unchanged', 'models': 'unchanged',
        'images': sorted(name for name in base['images'] if name != 'frontend')}
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['build', 'package'])
    for name in ('source', 'base-source', 'base-release', 'trusted-key', 'gates', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--node-bin', type=Path, default=Path('/opt/node-v22.21.1-linux-x64/bin'))
    parser.add_argument('--signing-key', type=Path)
    parser.add_argument('--browser-proof', type=Path)
    parser.add_argument('--interaction-proof', type=Path)
    args = parser.parse_args()
    source = verify_source(args.source, committed=True)
    base = verify_release(args.base_release, args.trusted_key)
    changed = validate_sources(base, source, verify_source(args.base_source, committed=True))
    image = 'c-check-frontend:0.1.0-rc.23'
    env = dict(os.environ, PATH=str(args.node_bin) + ':' + os.environ['PATH'],
               TMPDIR='/dev/shm', PYTHONDONTWRITEBYTECODE='1', VITE_USE_MOCK_API='false')
    def run(command, name, cwd=None):
        print('RUN', name, flush=True)
        with (args.gates / (name + '.log')).open('wb') as log:
            subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=1800)
    if args.phase == 'build':
        args.gates.mkdir(mode=0o700, parents=True, exist_ok=False)
        frontend = args.gates / 'frontend'
        shutil.copytree(args.source / 'frontend', frontend)
        run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(args.source / 'deploy/release/tests'), '-v'], 'tool-tests')
        run([str(args.node_bin / 'npm'), 'ci', '--cache', '/mnt/c-check-build/cache/npm'], 'npm-ci', frontend)
        run([str(args.node_bin / 'npm'), 'test'], 'frontend-tests', frontend)
        run([str(args.node_bin / 'npm'), 'run', 'build'], 'frontend-build', frontend)
        old = locate(base['images']['frontend'])
        if old is None:
            raise RuntimeError('signed rc22 frontend image is not loaded')
        tag = 'c-check-rc23-base-frontend:' + old.split(':')[-1][:16]
        subprocess.run(['docker', 'tag', old, tag], check=True)
        run(['docker', 'build', '--network', 'none', '--provenance=false', '--sbom=false',
             '--build-arg', 'BASE_IMAGE=' + tag, '--build-arg', 'RELEASE_VERSION=' + VERSION,
             '--build-arg', 'SOURCE_SHA=' + source['git_sha'], '-f',
             str(args.source / 'deploy/release/Dockerfile.frontend-app-patch'), '-t', image, str(frontend)], 'frontend-image')
        frontend_smoke(image, frontend / 'dist')
        policy = json.loads((args.source / 'deploy/release/core-policy.json').read_text())
        audit = inspect_layers(image, policy, {source['files']['backend/' + name.replace('.', '/') + '.py']
                                             for name in policy['compiled_modules']})
        meta = inspect_image(image)
        dist_files = {path.relative_to(frontend / 'dist').as_posix(): digest(path)
                      for path in (frontend / 'dist').rglob('*') if path.is_file()}
        report = {'passed': True, 'version': VERSION, 'source_tree': source['tree_sha256'],
            'source': source['git_sha'], 'base_release_sha256': base['_sha256'], 'changed_sources': changed,
            'frontend_image': meta['Id'], 'frontend_identity': identity(meta), 'layer_audit': audit,
            'tool_tests': True, 'frontend_tests': True, 'readonly_nonroot_http': True,
            'gpu_tested': False, 'backend_schema_models_inference_unchanged': True,
            'dist_files': dist_files, 'dist_tree': hashlib.sha256(json.dumps(dist_files, sort_keys=True).encode()).hexdigest()}
        (args.gates / 'build-gates.json').write_text(json.dumps(report, indent=2))
        print('RC23_FRONTEND_CANDIDATE_READY', json.dumps(report), flush=True)
        return
    report = json.loads((args.gates / 'build-gates.json').read_text())
    if not report['passed'] or report['source_tree'] != source['tree_sha256'] or report['version'] != VERSION:
        raise RuntimeError('build gates do not match source/version')
    meta = inspect_image(image)
    if meta['Id'] != report['frontend_image'] or identity(meta) != report['frontend_identity']:
        raise RuntimeError('candidate image changed')
    for proof in (args.browser_proof, args.interaction_proof):
        data = json.loads(proof.read_text())
        if (not data.get('passed') or {row['width'] for row in data['results']} != {1440, 768, 390}
                or data.get('tested_dist_tree') != report['dist_tree']):
            raise RuntimeError('desktop/tablet/mobile browser acceptance missing')
    key = signing_key(args.signing_key, [args.source, args.output, args.gates, args.base_release])
    output = args.output
    if output.exists() or output.name != 'release-' + VERSION:
        raise RuntimeError('new correctly named rc23 output required')
    output.mkdir(mode=0o700, parents=True)
    for name in base['files']:
        if name not in METADATA | {'README.md', 'INSTALL-RTX5880.md', 'verify/build-gates.json'} and not name.startswith('images/frontend.'):
            reuse_file(args.base_release / name, output / name, image=name.startswith('images/'))
    shutil.copy2(args.base_release / 'verify/build-gates.json', output / 'verify/rc22-build-gates.json')
    shutil.copy2(args.gates / 'build-gates.json', output / 'verify/build-gates.json')
    shutil.copy2(args.browser_proof, output / 'verify/assistant-ui-browser.json')
    shutil.copy2(args.interaction_proof, output / 'verify/assistant-interactions-browser.json')
    public = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'])
    if hashlib.sha256(public).hexdigest() != base['_publisher']:
        raise RuntimeError('original trusted publisher key required')
    (output / 'README.md').write_text('# C-Check ' + VERSION + '\n\nSigned assistant UI patch from rc22. '
        'Only the frontend image changes. Backend, schema, models, inference, CLI and signed GPU launchers remain unchanged. '
        'Read INSTALL-RTX5880.md; use update with an encrypted backup, not install on an existing instance.\n')
    (output / 'INSTALL-RTX5880.md').write_text(render_install_guide(
        (args.source / 'deploy/release/INSTALL-ASSISTANT-UI-PATCH.template.md').read_text(), VERSION, base['_publisher']))
    parts = export_image(image, output / 'images', 'frontend')
    frontend = {'id': meta['Id'], 'size_bytes': meta['Size'], 'filesystem_config_sha256': identity(meta), 'archive_parts': parts}
    bootstrap(output)
    release = package_metadata(base, source, output, frontend)
    release['validation'] = {'report': 'verify/build-gates.json', 'passed': True, 'gpu_tested': False,
                             'scope': 'CPU frontend build, HTTP/image audit, browser API fixtures; no new GPU acceptance'}
    (output / 'release.json').write_text(json.dumps(release, indent=2) + '\n')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in',
                    str(output / 'release.json'), '-out', str(output / 'release.sig')], check=True)
    verified = verify_release(output, args.trusted_key, base['_publisher'])
    for field in ('runtime_launchers', 'assistant_runtime', 'terminal_runtime', 'schema_epoch', 'schema_files', 'core_wheel'):
        if verified[field] != base[field]:
            raise RuntimeError('protected runtime metadata changed: ' + field)
    subprocess.run([sys.executable, '-I', '-c',
        "import sys,json;sys.path.insert(0,sys.argv[1]);from ccheck import acceleration,assistant;"
        "r=json.load(open(sys.argv[2]));s={'profile':'ada-48g-fp8','performance':{},'assistant':{'enabled':True}};"
        "acceleration.validate_release(s,r);assistant.validate_release(s,r)",
        str(output / 'cli/ccheck.pyz'), str(output / 'release.json')], check=True)
    if verify_source(args.source, committed=True) != source:
        raise RuntimeError('source changed during packaging')
    bundle = patch_bundle(output, verified)
    print('RC23_SIGNED_FRONTEND_PATCH', verified['_sha256'], str(bundle), bundle.stat().st_size, flush=True)


if __name__ == '__main__':
    main()
