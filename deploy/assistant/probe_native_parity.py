"""Run the same backend tests against source and native candidate without networking."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    sys.path.insert(0, str(source / 'deploy/release'))
    from validate_images import cases
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image], timeout=30))[0]['Id']
    for kind in ('source', 'compiled'):
        name = 'ccheck-assistant-parity-' + uuid.uuid4().hex
        mount = source / ('backend' if kind == 'source' else 'backend/tests')
        target = '/app/backend' if kind == 'source' else '/app/backend/tests'
        command = ['docker', 'run', '--rm', '--name', name, '--network', 'none',
            '--user', '0:0', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--memory', '3g', '--cpus', '2',
            '--tmpfs', '/tmp:size=536870912,mode=1777', '--entrypoint', 'python',
            '-e', 'ALLOW_INSECURE_DEFAULTS=true', '-e', 'RAG_EMBEDDING_BACKEND=hashing',
            '-e', 'RAG_QDRANT_URL=', '-e', 'RAG_PARSER_REQUIRE_TREE_SITTER=false',
            '-e', 'RAG_PARSER_REQUIRE_LIBCLANG=false', '-e', 'PYTHONDONTWRITEBYTECODE=1',
            '-e', 'LD_LIBRARY_PATH=/usr/lib/llvm-14/lib',
            '-v', str(args.output.resolve()) + ':/results', '-v', str(mount) + ':' + target + ':ro',
            '-v', str(source / 'deploy') + ':/app/deploy:ro',
            '-v', str(source / 'tools') + ':/app/tools:ro',
            '-w', '/app/backend', image, '-B', '-m', 'pytest', '/app/backend/tests',
            '-q', '-p', 'no:cacheprovider', '--junitxml=/results/' + kind + '.xml']
        print('RUN_BACKEND_PARITY', kind, flush=True)
        try:
            with (args.output / (kind + '.log')).open('wb') as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
        finally:
            subprocess.run(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
    source_cases = cases(args.output / 'source.xml')
    compiled_cases = cases(args.output / 'compiled.xml')
    if not source_cases or source_cases != compiled_cases:
        raise RuntimeError('source/native test inventory or outcomes differ')
    report = {'passed': True, 'image_id': image, 'cases': len(source_cases), 'network': 'none'}
    (args.output / 'parity.json').write_text(json.dumps(report, indent=2))
    print('ASSISTANT_SOURCE_NATIVE_PARITY_OK', json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
