"""Build and validate the rc16-based application patch without a GPU or model downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from build_application_patch import VERSION, validate_sources
from repack_deployment import verify_source, read_json, write_json
from ccheck.core import verify_release
from ccheck.image_identity import locate, identity, inspect_image
from audit_images import inspect_layers
from validate_images import cases


def main(args):
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = verify_source(source, committed=True)
    base = verify_release(args.base_release, args.trusted_key)
    policy = validate_sources(base, frozen, verify_source(args.base_source, committed=True), source)
    old_backend, old_frontend = (locate(base['images'][name]) for name in ('backend', 'frontend'))
    if not old_backend or not old_frontend:
        raise RuntimeError('signed rc16 baseline images must already be imported')
    base_tags = {}
    for name, image in [('backend', old_backend), ('frontend', old_frontend)]:
        base_tags[name] = 'c-check-rc18-base-' + name + ':' + image.split(':')[-1][:16]
        subprocess.run(['docker', 'tag', image, base_tags[name]], check=True)

    def run(command, name, **kwargs):
        print('RUN', name, flush=True)
        with (output / (name + '.log')).open('wb') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800, **kwargs)

    context = output / 'backend-context'
    for name in policy['plaintext_allowlist'] + policy['resource_allowlist']:
        target = context / 'backend' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / 'backend' / name, target)
    shutil.copytree(source / 'backend/alembic', context / 'backend/alembic')
    shutil.copyfile(source / 'backend/alembic.ini', context / 'backend/alembic.ini')
    shutil.copyfile(source / 'deploy/release/core-policy.json', context / 'core-policy.json')
    images = {'backend': 'c-check-backend:0.1.0-rc.18', 'frontend': 'c-check-frontend:0.1.0-rc.18'}
    run(['docker', 'build', '--network', 'none', '--provenance=false', '--sbom=false', '--build-arg', 'BASE_IMAGE=' + base_tags['backend'],
         '--build-arg', 'RELEASE_VERSION=' + VERSION, '--build-arg', 'SOURCE_SHA=' + frozen['git_sha'],
         '-f', str(source / 'deploy/release/Dockerfile.backend-app-patch'), '-t', images['backend'], str(context)], 'backend-build')
    common = ['docker', 'run', '--rm', '--network', 'none', '--user', '0:0', '--memory', '3g', '--cpus', '2',
              '--entrypoint', 'python', '-e', 'ALLOW_INSECURE_DEFAULTS=true', '-e', 'RAG_EMBEDDING_BACKEND=hashing',
              '-e', 'RAG_QDRANT_URL=', '-e', 'RAG_PARSER_REQUIRE_TREE_SITTER=false', '-e', 'RAG_PARSER_REQUIRE_LIBCLANG=false',
              '-e', 'LD_LIBRARY_PATH=/usr/lib/llvm-14/lib', '-e', 'PYTHONDONTWRITEBYTECODE=1',
              '-v', str(output) + ':/results', '-w', '/app/backend']
    for kind, image in [('source', old_backend), ('compiled', images['backend'])]:
        mount = source / ('backend' if kind == 'source' else 'backend/tests')
        target = '/app/backend' if kind == 'source' else '/app/backend/tests'
        run(common + ['-v', str(mount) + ':' + target + ':ro', image, '-m', 'pytest', '/app/backend/tests',
                      '-q', '-p', 'no:cacheprovider', '--junitxml=/results/' + kind + '.xml'], kind + '-tests')
    source_cases, native_cases = cases(output / 'source.xml'), cases(output / 'compiled.xml')
    if not source_cases or source_cases != native_cases:
        raise RuntimeError('source/native test inventory or outcome mismatch')
    run(common + [images['backend'], '-m', 'app.deployment', 'audit-core'], 'core-audit')
    expected = {row['file']: row['sha256'] for row in base['core_wheel']['modules']}
    program = ('import hashlib,json;from pathlib import Path;files=' + repr(list(expected)) + ';'
               'print(json.dumps({p:hashlib.sha256((Path("/app/backend")/p).read_bytes()).hexdigest() for p in files}))')
    actual = json.loads(subprocess.check_output(common + [images['backend'], '-c', program]))
    if actual != expected:
        raise RuntimeError('compiled core changed')
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(source / 'deploy/release/tests'), '-v'], 'tool-tests')

    front = output / 'frontend-context'
    front.mkdir()
    # The pinned test image provides the rc16 lockfile's dependencies, used offline.
    check = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat',
                                     args.frontend_tools, '/source/package-lock.json'])
    if hashlib.sha256(check).hexdigest() != frozen['files']['frontend/package-lock.json']:
        raise RuntimeError('frontend tools lockfile differs from frozen source')
    run(['docker', 'run', '--rm', '--network', 'none', '--memory', '4g', '--cpus', '2', '--entrypoint', 'sh',
         '-v', str(source / 'frontend') + ':/input:ro', '-v', str(front) + ':/output', args.frontend_tools,
         '-ec', 'mkdir /tmp/frontend; cp -a /input/. /tmp/frontend/; ln -s /source/node_modules /tmp/frontend/node_modules; '
         'cd /tmp/frontend; npm test; npm run build; cp -a dist /output/'], 'frontend-tests-build')
    shutil.copyfile(source / 'frontend/nginx.conf', front / 'nginx.conf')
    run(['docker', 'build', '--network', 'none', '--provenance=false', '--sbom=false', '--build-arg', 'BASE_IMAGE=' + base_tags['frontend'],
         '--build-arg', 'RELEASE_VERSION=' + VERSION, '--build-arg', 'SOURCE_SHA=' + frozen['git_sha'],
         '-f', str(source / 'deploy/release/Dockerfile.frontend-app-patch'), '-t', images['frontend'], str(front)], 'frontend-build')
    private = {frozen['files']['backend/' + name.replace('.', '/') + '.py'] for name in policy['compiled_modules']}
    audits = {name: inspect_layers(image, policy, private) for name, image in images.items()}
    if set(audits['backend']['native_modules']) != set(policy['compiled_modules']):
        raise RuntimeError('compiled modules missing')
    write_json(output / 'layer-audit.json', audits)
    migration = mysql_migration(base, images['backend'], old_backend, run)
    report = {'passed': True, 'source_tree': frozen['tree_sha256'], 'git_sha': frozen['git_sha'],
              'backend': {'passed': True, 'compiled_core_unchanged': True, 'cases': len(source_cases),
                          'image_identity': identity(inspect_image(images['backend'])), 'native_sha256': actual},
              'frontend': {'passed': True, 'image_identity': identity(inspect_image(images['frontend']))},
              'tool_tests': {'passed': True}, 'security': {'passed': True, 'audits': audits},
              'migration': migration, 'gpu_tested': False, 'model_runtime': 'unchanged from signed rc16'}
    if verify_source(source, committed=True) != frozen:
        raise RuntimeError('frozen source changed during tests')
    write_json(output / 'images.json', images)
    write_json(output / 'build-gates.json', report)
    print('APPLICATION_GATES_PASSED', json.dumps(report), flush=True)


def mysql_migration(base, new_backend, old_backend, run):
    image = locate(base['images']['mysql'])
    if not image:
        raise RuntimeError('signed MySQL image must be imported for migration test')
    name = 'ccheck-rc18-migration-' + secrets.token_hex(5)
    password = secrets.token_hex(24)
    env = dict(os.environ, MYSQL_ROOT_PASSWORD=password,
               DATABASE_URL='mysql+pymysql://root:' + password + '@127.0.0.1:3306/ccheck_patch')
    subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none', '--memory', '1g', '--cpus', '1',
                    '-e', 'MYSQL_ROOT_PASSWORD', '-e', 'MYSQL_ROOT_HOST=%', '-e', 'MYSQL_DATABASE=ccheck_patch', image],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    try:
        for _ in range(90):
            probe = subprocess.run(['docker', 'exec', name, 'mysqladmin', 'ping', '--silent'], capture_output=True)
            if probe.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError('isolated MySQL startup timeout')
        common = ['docker', 'run', '--rm', '--network', 'container:' + name, '--memory', '1g', '--cpus', '1',
                  '--entrypoint', 'python', '-e', 'DATABASE_URL', '-e', 'ALLOW_INSECURE_DEFAULTS=true',
                  '-e', 'PYTHONDONTWRITEBYTECODE=1', '-w', '/app/backend']
        run(common + [old_backend, '-m', 'app.deployment', 'migrate'], 'mysql-old-schema', env=env)
        seed = ('from app.db.session import SessionLocal;from app.db.models import User,ModelNode,ReviewTask,ReviewFile,TaskStatus;'
                's=SessionLocal();u=User(username="migration-sentinel",password_hash="keep-existing-hash");'
                'm=ModelNode(display_name="old-model",model_identifier="qwen2.5-coder-32b-instruct-fp8",base_url="http://unused");'
                't=ReviewTask(id="rc18-sentinel",owner=u,model_node=m,input_mode="file",display_name="keep.c",'
                'status=TaskStatus.COMPLETED,progress=100,duration_ms=123,finding_count=0);'
                't.files=[ReviewFile(relative_path="keep.c",source_text="int sentinel;",size_bytes=13)];'
                's.add(t);s.commit()')
        run(common + [old_backend, '-c', seed], 'mysql-seed', env=env)
        run(common + [new_backend, '-m', 'app.deployment', 'migrate'], 'mysql-migrate', env=env)
        verify = ('from app.db.session import SessionLocal;from app.db.models import ReviewTask;'
                  's=SessionLocal();t=s.get(ReviewTask,"rc18-sentinel");'
                  'assert t.graph_build_status=="idle" and t.graph_build_id is None and t.graph_build_error is None;'
                  'assert t.progress==100 and t.duration_ms==123 and t.files[0].source_text=="int sentinel;";'
                  'assert t.owner.password_hash=="keep-existing-hash";'
                  'assert t.model_node.model_identifier=="qwen2.5-coder-32b-instruct-fp8";'
                  'print("MYSQL_MIGRATION_PRESERVED")')
        run(common + [new_backend, '-c', verify], 'mysql-preservation', env=env)
        run(common + [new_backend, '-m', 'app.deployment', 'migrate'], 'mysql-repeat', env=env)
        return {'passed': True, 'mysql': True, 'repeatable': True, 'existing_rows_and_password_preserved': True}
    finally:
        subprocess.run(['docker', 'rm', '-f', '-v', name], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('source', 'output', 'base-release', 'base-source', 'trusted-key'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--frontend-tools', required=True)
    main(parser.parse_args())
