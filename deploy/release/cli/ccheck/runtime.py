import json
import hashlib
from pathlib import Path
import re
import shutil

from .core import DeploymentError, atomic_json, checked_path, digest, run
from .models import obtain_file


def runtime_recipe_identity(release):
    specification = release.get('runtime_recipe')
    if not specification:
        raise DeploymentError('this release does not supply a signed runtime build recipe')
    prefix = checked_path(release['_root'], specification['directory'])
    fingerprint = hashlib.sha256(json.dumps({'base_image': specification['base_image'],
        'files': {name: digest(prefix / name) for name in ['requirements.lock', 'Dockerfile', 'qwen_entry.py', 'wheels.json']}},
        sort_keys=True).encode()).hexdigest()
    return prefix, fingerprint


def cached_runtime_image(release, site):
    _, fingerprint = runtime_recipe_identity(release)
    marker = checked_path(site['data_dir'], 'state/runtime-build.json')
    try:
        old = json.loads(marker.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(old, dict) or old.get('recipe') != fingerprint:
        return None
    image = old.get('image')
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        return None
    try:
        actual = run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}']).strip()
    except DeploymentError:
        return None
    return actual if actual == image else None


def build_runtime(release, site):
    cached = cached_runtime_image(release, site)
    if cached:
        return cached
    prefix, fingerprint = runtime_recipe_identity(release)
    specification = release['runtime_recipe']
    root = Path(site['data_dir'])
    marker = root / 'state/runtime-build.json'
    context = root / 'data/cache/runtime-build'
    wheels = context / 'wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    records = json.loads((prefix / 'wheels.json').read_text())
    for entry in records:
        obtain_file(entry, checked_path(wheels, entry['file']))
    for name in ('Dockerfile', 'requirements.lock', 'qwen_entry.py'):
        shutil.copy2(prefix / name, context / name)
    tag = 'ccheck-' + site['instance'] + '-runtime:' + release['version'].lstrip('v')
    run(['docker', 'build', '--build-arg', 'BASE_IMAGE=' + specification['base_image'], '-t', tag, str(context)],
        timeout=7200, capture=False)
    image = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
    probe = run(['docker', 'run', '--rm', '--gpus', 'device=' + str(site['gpu']), '--entrypoint', 'python', image,
                 '-c', "import torch,vllm,transformers,json;assert torch.cuda.is_available();"
                       "assert torch.__version__.split('+')[0]=='2.6.0';assert torch.version.cuda=='12.4';"
                       "assert vllm.__version__=='0.8.5.post1';assert transformers.__version__=='4.51.3';"
                       "print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'vllm':vllm.__version__}))"], timeout=120)
    atomic_json(marker, {'image': image, 'recipe': fingerprint, 'probe': probe.strip(), 'release': release['_sha256']})
    return image
