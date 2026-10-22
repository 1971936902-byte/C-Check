import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

from audit_images import inspect_layers
from build_images import pinned
from prepare_build import sha

GATE_RUN_ID = uuid.uuid4().hex


def cleanup_gate_containers():
    result = subprocess.run(['docker', 'ps', '-a', '-q', '--filter', 'label=io.ccheck.release-gate=' + GATE_RUN_ID],
                            text=True, capture_output=True, check=True, timeout=30)
    ids = result.stdout.splitlines()
    if any(not re.fullmatch(r'[0-9a-f]{12,64}', value) for value in ids):
        raise RuntimeError('unexpected container identity in gate cleanup')
    if ids:
        subprocess.run(['docker', 'rm', '--force', *ids], check=True, timeout=60)


def execute(command, log):
    if command[:2] == ['docker', 'run']:
        command = command[:2] + ['--label', 'io.ccheck.release-gate=' + GATE_RUN_ID] + command[2:]
    with log.open('wb') as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True)


def cases(path):
    result = {}
    for case in ET.parse(path).getroot().iter('testcase'):
        key = (case.get('classname', '').rsplit('.', 1)[-1], case.get('name'))
        if case.find('failure') is not None or case.find('error') is not None:
            raise RuntimeError('deterministic test failure: ' + str(key))
        result[key] = 'skipped' if case.find('skipped') is not None else 'passed'
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('context', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    context, output = args.context.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    built = json.loads((context / 'built-images.json').read_text())
    policy = json.loads((context / 'deploy/release/core-policy.json').read_text())
    source = json.loads((context / 'build-source.json').read_text())
    images = {name: entry['id'] for name, entry in built.items()}
    if not {'backend', 'frontend', 'embedding', 'qwen'}.issubset(images):
        raise RuntimeError('all application/runtime images must be built')
    baseline = 'c-check-test-baseline:' + args.version.lstrip('v')
    execute(['docker', 'build', '-t', baseline, '--target', 'dependencies',
             '--build-arg', 'BASE_IMAGE=' + pinned('python:3.12-slim-bookworm'),
             '-f', str(context / 'deploy/release/Dockerfile.backend'), str(context)], output / 'baseline-build.log')
    common = ['docker', 'run', '--rm', '--user', '0:0', '--entrypoint', 'python',
              '-e', 'ALLOW_INSECURE_DEFAULTS=true', '-e', 'ADMIN_USERNAME=admin', '-e', 'ADMIN_PASSWORD=change-this-password',
              '-e', 'RAG_EMBEDDING_BACKEND=hashing', '-e', 'RAG_QDRANT_URL=',
              '-e', 'RAG_PARSER_REQUIRE_TREE_SITTER=false', '-e', 'RAG_PARSER_REQUIRE_LIBCLANG=false',
              '-e', 'LD_LIBRARY_PATH=/usr/lib/llvm-14/lib',
              '--mount', 'type=bind,src=' + str(context / 'deploy/models') + ',dst=/app/deploy/models,readonly',
              '--mount', 'type=bind,src=' + str(output) + ',dst=/results']
    for kind in ('baseline', 'compiled'):
        if kind == 'baseline':
            command = common + ['--mount', 'type=bind,src=' + str(context / 'backend') + ',dst=/app/backend,readonly',
                                '-w', '/app/backend', baseline]
            tests = '/app/backend/tests'
        else:
            command = common + ['--mount', 'type=bind,src=' + str(context / 'backend/tests') + ',dst=/app/backend/tests,readonly',
                                '-w', '/app/backend', images['backend']]
            tests = '/app/backend/tests'
        execute(command + ['-m', 'pytest', tests, '-q', '-p', 'no:cacheprovider', '--junitxml=/results/' + kind + '.xml'],
                output / (kind + '-tests.log'))
    baseline_cases, compiled_cases = cases(output / 'baseline.xml'), cases(output / 'compiled.xml')
    if baseline_cases != compiled_cases or not baseline_cases:
        raise RuntimeError('baseline/native deterministic test inventories or outcomes differ')
    execute(common + ['--mount', 'type=bind,src=' + str(context / 'deploy/release') + ',dst=/release-tests,readonly',
                      images['backend'], '-m', 'unittest', 'discover', '-s', '/release-tests/tests', '-v'],
            output / 'cli-tests.log')
    execute(common + [images['backend'], '-m', 'app.deployment', 'audit-core'], output / 'native-core.log')
    execute(['docker', 'run', '--rm', '--user', '10017:10017', '--read-only',
             '--tmpfs', '/tmp:rw,mode=1777', '--entrypoint', 'python', images['backend'], '-c',
             "from pathlib import Path;from alembic.config import Config;"
             "from alembic.script import ScriptDirectory;root=Path('/app/backend');"
             "files=[p for p in root.rglob('*') if p.is_file()];"
             "[(p.open('rb').read(1)) for p in files];"
             "assert (root/'alembic/env.py').read_bytes();"
             "heads=ScriptDirectory.from_config(Config(str(root/'alembic.ini'))).get_heads();"
             "assert heads;print('nonroot_runtime_files_readable',len(files),'migration_heads',heads)"],
            output / 'nonroot-backend-artifacts.log')
    frontend_test_image = 'c-check-frontend-tests:' + args.version.lstrip('v')
    execute(['docker', 'build', '-t', frontend_test_image, '--target', 'builder',
             '--build-arg', 'NODE_IMAGE=' + pinned('node:22.21.1-bookworm-slim'),
             '--build-arg', 'NGINX_IMAGE=' + pinned('nginx:1.28-alpine'),
             '-f', str(context / 'deploy/release/Dockerfile.frontend'), str(context)], output / 'frontend-test-build.log')
    execute(['docker', 'run', '--rm', frontend_test_image, 'npm', 'test', '--', '--run'], output / 'frontend-tests.log')
    execute(['docker', 'run', '--rm', '--gpus', 'device=0', '--entrypoint', 'python', images['qwen'], '-c',
             "import torch,vllm,transformers;assert torch.cuda.is_available();"
             "assert torch.__version__.split('+')[0]=='2.6.0';assert torch.version.cuda=='12.4';"
             "assert vllm.__version__=='0.8.5.post1';assert transformers.__version__=='4.51.3';"
             "print(torch.__version__,torch.version.cuda,vllm.__version__,transformers.__version__)"], output / 'gpu-runtime.log')
    execute(['docker', 'run', '--rm', '--gpus', 'device=0', '--user', '10017:10017', '--read-only',
             '--tmpfs', '/tmp:rw,exec,mode=1777', '-e', 'HOME=/tmp', '-e', 'TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor',
             '-e', 'VLLM_CACHE_ROOT=/tmp/vllm', '-e', 'TRITON_CACHE_DIR=/tmp/triton',
             '--entrypoint', 'python', images['qwen'], '-c',
             "import getpass,os;assert getpass.getuser()=='ccheck';"
             "from vllm.model_executor.models import ModelRegistry;"
             "info,architecture=ModelRegistry.inspect_model_cls(['Qwen2ForCausalLM']);"
             "assert info is not None and architecture=='Qwen2ForCausalLM';"
             "from triton.backends.nvidia.driver import CudaUtils;CudaUtils();"
             "print('nonroot_model_registry',os.getuid(),getpass.getuser(),architecture)"], output / 'nonroot-model-registry.log')
    execute(['docker', 'run', '--rm', '--entrypoint', 'python', images['embedding'], '-c',
             "import torch,transformers;assert torch.__version__=='2.7.1+cpu';assert transformers.__version__=='4.53.3';print(torch.__version__,transformers.__version__)"],
            output / 'embedding-runtime.log')
    private_hashes = {sha(context / 'backend' / (name.replace('.', '/') + '.py')) for name in policy['compiled_modules']}
    layer_reports = []
    for name, image in images.items():
        report = inspect_layers(image, policy, private_hashes)
        if name == 'backend' and set(report['native_modules']) != set(policy['compiled_modules']):
            raise RuntimeError('missing core native modules')
        layer_reports.append(report)
    # A deleted source file must still be detected in an earlier layer.
    negative = output / 'negative-context'
    negative.mkdir(exist_ok=True)
    (negative / 'private.py').write_bytes((context / 'backend/app/services/model_router.py').read_bytes())
    negative_base = built['backend']['tag']
    resolved = subprocess.check_output(['docker', 'image', 'inspect', negative_base, '--format', '{{.Id}}'], text=True).strip()
    if resolved != images['backend']:
        raise RuntimeError('negative audit base tag no longer names the tested backend image')
    (negative / 'Dockerfile').write_text('FROM ' + negative_base + '\nUSER 0:0\nCOPY private.py /tmp/renamed-private.py\nRUN rm /tmp/renamed-private.py\nUSER 10001:10001\n')
    negative_image = 'c-check-negative-audit:' + args.version.lstrip('v')
    execute(['docker', 'build', '--pull=false', '-t', negative_image, str(negative)], output / 'negative-build.log')
    detected = False
    try:
        inspect_layers(negative_image, policy, private_hashes)
    except RuntimeError as exc:
        detected = 'private source' in str(exc) or 'private Python' in str(exc)
    subprocess.run(['docker', 'image', 'rm', negative_image], check=True)
    if not detected:
        raise RuntimeError('negative image-layer source leakage gate failed')
    cleanup_gate_containers()
    result = {'passed': True, 'gate_run_id': GATE_RUN_ID, 'source_tree': source['tree_sha256'], 'version': args.version, 'images': images,
              'core_policy_sha256': sha(context / 'deploy/release/core-policy.json'),
              'deterministic_tests': len(baseline_cases), 'layer_reports': layer_reports,
              'negative_deleted_layer_detected': detected}
    (output / 'gates.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    try:
        main()
    except BaseException:
        cleanup_gate_containers()
        raise
