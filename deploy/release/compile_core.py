"""Runs only inside the trusted builder; never copy this/source into runtime layers."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

source = Path('/src/backend')
output = Path('/artifacts/backend')
policy = json.loads(Path('/policy/core-policy.json').read_text())
output.mkdir(parents=True)
for name in policy['compiled_modules']:
    relative = name.replace('.', '/') + '.py'
    work = Path('/tmp/compile') / name
    work.mkdir(parents=True)
    subprocess.run([sys.executable, '-m', 'nuitka', '--module', str(source / relative),
                    '--output-dir=' + str(work), '--no-pyi-file', '--remove-output',
                    '--disable-ccache', '--jobs=8'], check=True, cwd=source)
    extensions = list(work.glob('*.so'))
    if len(extensions) != 1:
        raise RuntimeError('Expected one native extension: ' + name)
    target = output / Path(relative).parent / extensions[0].name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(extensions[0], target)
    subprocess.run(['strip', '--strip-debug', str(target)], check=True)
for prohibited in ('*.pyc', '*.c', '*.cpp', '*.pdb', '*.o'):
    if list(output.rglob(prohibited)):
        raise RuntimeError('Prohibited first-party artifact: ' + prohibited)
print('CORE_COMPILATION_COMPLETE', len(policy['compiled_modules']))
