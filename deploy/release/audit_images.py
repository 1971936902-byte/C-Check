import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile


def inspect_layers(image, policy, private_hashes):
    prohibited = {name.replace('.', '/') + '.py' for name in policy['compiled_modules']}
    allowed = set(policy['plaintext_allowlist'])
    layers, native = 0, set()
    with tempfile.TemporaryDirectory(prefix='ccheck-image-audit-') as temporary:
        archive = Path(temporary) / 'image.tar'
        subprocess.run(['docker', 'image', 'save', '--output', str(archive), image], check=True)
        with tarfile.open(archive, 'r:') as outer:
            manifest = json.load(outer.extractfile('manifest.json'))
            layer_paths = {path for entry in manifest for path in entry['Layers']}
            for layer_path in sorted(layer_paths):
                stream = outer.extractfile(layer_path)
                with tarfile.open(fileobj=stream, mode='r:*') as layer:
                    layers += 1
                    for member in layer:
                        name = member.name
                        while name.startswith('./'):
                            name = name[2:]
                        parts = PurePosixPath(name).parts
                        if name.startswith('/') or '..' in parts:
                            raise RuntimeError('unsafe layer member path')
                        if '.git' in parts or name.startswith(('src/backend/', 'builder/', 'root/.ssh/')):
                            raise RuntimeError('prohibited source/build/private directory in image layer: ' + name)
                        if any(name.endswith(path) for path in prohibited):
                            raise RuntimeError('private Python source exists in an image layer: ' + name)
                        prefix = 'app/backend/'
                        if name.startswith(prefix):
                            relative = name[len(prefix):]
                            if relative.startswith('app/') and relative.endswith('.py') and relative not in allowed:
                                raise RuntimeError('first-party Python outside explicit allowlist: ' + relative)
                            if relative.endswith(('.pyc', '.c', '.cpp', '.o', '.pdb', '.zip', '.tar', '.gz')) or '/.env' in name:
                                raise RuntimeError('prohibited first-party artifact in layer: ' + name)
                            for module in policy['compiled_modules']:
                                path = module.replace('.', '/')
                                if relative.startswith(path + '.') and relative.endswith('.so'):
                                    native.add(module)
                        if name.startswith('usr/share/nginx/html/') and name.endswith('.map'):
                            raise RuntimeError('frontend source map in image layer')
                        if member.isfile() and member.size < 8 * 1024 * 1024:
                            content = layer.extractfile(member).read()
                            if hashlib.sha256(content).hexdigest() in private_hashes:
                                raise RuntimeError('renamed copy of private source exists in image layer: ' + name)
    return {'image': image, 'layers_scanned': layers, 'native_modules': sorted(native)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('image')
    parser.add_argument('--policy', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--backend', action='store_true')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    private_hashes = {hashlib.sha256((args.source / (name.replace('.', '/') + '.py')).read_bytes()).hexdigest()
                      for name in policy['compiled_modules']}
    result = inspect_layers(args.image, policy, private_hashes)
    if args.backend and set(result['native_modules']) != set(policy['compiled_modules']):
        raise RuntimeError('not all core native modules are present in final image layers')
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
