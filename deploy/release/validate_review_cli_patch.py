"""Build rc19 in an isolated Docker daemon; no model downloads or GPU invocation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from build_review_cli_patch import VERSION, CHANGED_CORE, validate_sources
from repack_deployment import verify_source, read_json, write_json
from ccheck.core import verify_release
from ccheck.image_identity import locate, identity, inspect_image
from audit_images import inspect_layers
from validate_images import cases
from validate_app_patch import mysql_migration


def main(args):
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = verify_source(source, committed=True)
    base = verify_release(args.base_release, args.trusted_key)
    policy = validate_sources(base, frozen, verify_source(args.base_source, committed=True), source)
    old_backend = locate(base['images']['backend'])
    if not old_backend:
        raise RuntimeError('signed rc18 backend must be imported into the isolated build daemon')

    def run(command, name, **kwargs):
        print('RUN', name, flush=True)
        with (output/(name+'.log')).open('wb') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=3600, **kwargs)

    context = output/'backend-context'
    for name in policy['plaintext_allowlist'] + policy['resource_allowlist']:
        target = context/'backend'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/'backend'/name, target)
    shutil.copytree(source/'backend/alembic', context/'backend/alembic')
    shutil.copyfile(source/'backend/alembic.ini', context/'backend/alembic.ini')
    shutil.copyfile(source/'deploy/release/core-policy.json', context/'core-policy.json')
    for module in CHANGED_CORE:
        relative = module.replace('.', '/')+'.py'
        target = context/'native-source'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/'backend'/relative, target)
    shutil.copyfile(source/'backend/app/__init__.py', context/'native-source/app/__init__.py')
    write_json(context/'native-policy.json', {'compiled_modules': sorted(CHANGED_CORE)})
    shutil.copyfile(source/'deploy/release/compile_core.py', context/'compile_core.py')
    shutil.copytree(args.compiler_inputs/'wheelhouse/compiler', context/'wheelhouse/compiler')
    (context/'locks').mkdir()
    shutil.copyfile(args.compiler_inputs/'locks/compiler.lock', context/'locks/compiler.lock')
    if read_json(args.base_release/'locks/compiler.wheels.json') != read_json(args.compiler_inputs/'locks/compiler.wheels.json'):
        raise RuntimeError('compiler wheels differ from signed baseline')
    if (args.base_release/'locks/compiler.lock').read_bytes() != (context/'locks/compiler.lock').read_bytes():
        raise RuntimeError('compiler lock differs from signed baseline')
    image = 'c-check-backend:0.1.0-rc.19'
    subprocess.run(['docker', 'tag', old_backend, 'ccheck-rc19-base:rc18'], check=True)
    run(['docker', 'build', '--network', 'host', '--provenance=false', '--sbom=false',
         '--build-arg', 'BASE_IMAGE=ccheck-rc19-base:rc18', '--build-arg', 'RELEASE_VERSION='+VERSION,
         '--build-arg', 'SOURCE_SHA='+frozen['git_sha'], '-f', str(source/'deploy/release/Dockerfile.backend-review-cli-patch'),
         '-t', image, str(context)], 'backend-native-build')
    common = ['docker', 'run', '--rm', '--network', 'none', '--user', '0:0', '--memory', '3g', '--cpus', '2',
              '--entrypoint', 'python', '-e', 'ALLOW_INSECURE_DEFAULTS=true', '-e', 'RAG_EMBEDDING_BACKEND=hashing',
              '-e', 'RAG_QDRANT_URL=', '-e', 'RAG_PARSER_REQUIRE_TREE_SITTER=false', '-e', 'RAG_PARSER_REQUIRE_LIBCLANG=false',
              '-e', 'LD_LIBRARY_PATH=/usr/lib/llvm-14/lib', '-e', 'PYTHONDONTWRITEBYTECODE=1',
              '-v', str(output)+':/results', '-v', str(source/'tools')+':/app/tools:ro', '-w', '/app/backend']

    def backend_tests(kind):
        mount = source/('backend' if kind == 'source' else 'backend/tests')
        target = '/app/backend' if kind == 'source' else '/app/backend/tests'
        run(common+['-v', str(mount)+':'+target+':ro', image, '-m', 'pytest', '/app/backend/tests',
                    '-q', '-p', 'no:cacheprovider', '--junitxml=/results/'+kind+'.xml'], kind+'-tests')

    with ThreadPoolExecutor(max_workers=2) as pool:
        for job in [pool.submit(backend_tests, kind) for kind in ('source', 'compiled')]:
            job.result()
    source_cases, native_cases = cases(output/'source.xml'), cases(output/'compiled.xml')
    if not source_cases or source_cases != native_cases:
        raise RuntimeError('source/native inventory or outcomes differ')
    run(common+[image, '-m', 'app.deployment', 'audit-core'], 'core-audit')
    expected = {row['file']: row['sha256'] for row in base['core_wheel']['modules']}
    program = ('import hashlib,json;from pathlib import Path;files='+repr(list(expected))+';'
               'print(json.dumps({p:hashlib.sha256((Path("/app/backend")/p).read_bytes()).hexdigest() for p in files}))')
    actual = json.loads(subprocess.check_output(common+[image, '-c', program]))
    changed = {row['module'] for row in base['core_wheel']['modules'] if actual[row['file']] != row['sha256']}
    if changed != CHANGED_CORE:
        raise RuntimeError('unexpected native artifact changes: '+str(changed))
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(source/'deploy/release/tests'), '-v'], 'tool-tests')
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(source/'tools/review-cli/tests'), '-v'], 'review-cli-tests')
    cli = output/'ccheck-review.pyz'
    run([sys.executable, '-B', str(source/'tools/review-cli/build_zipapp.py'), '--output', str(cli)], 'review-cli-build')
    run([sys.executable, '-B', str(cli), 'help'], 'review-cli-help')
    private = {frozen['files']['backend/'+name.replace('.', '/')+'.py'] for name in policy['compiled_modules']}
    audit = inspect_layers(image, policy, private)
    if set(audit['native_modules']) != set(policy['compiled_modules']):
        raise RuntimeError('native modules missing')
    migration = mysql_migration(base, image, old_backend, run, scan_progress=True)
    report = {'passed': True, 'source_tree': frozen['tree_sha256'], 'git_sha': frozen['git_sha'], 'base_sha256': base['_sha256'],
        'backend': {'passed': True, 'cases': len(source_cases), 'image_identity': identity(inspect_image(image)),
                    'native_sha256': actual, 'recompiled_modules': sorted(changed)},
        'frontend': {'passed': True, 'reused_signed_baseline': base['_sha256']},
        'tool_tests': {'passed': True}, 'review_cli': {'passed': True, 'version': '0.2.1'},
        'security': {'passed': True, 'backend_layers': audit}, 'migration': migration,
        'gpu_tested': False, 'model_runtime': 'unchanged from signed rc18'}
    if verify_source(source, committed=True) != frozen:
        raise RuntimeError('source changed during gates')
    write_json(output/'images.json', {'backend': image})
    write_json(output/'build-gates.json', report)
    print('RC19_GATES_PASSED', json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('source', 'output', 'base-release', 'base-source', 'trusted-key', 'compiler-inputs'):
        parser.add_argument('--'+name, type=Path, required=True)
    main(parser.parse_args())
