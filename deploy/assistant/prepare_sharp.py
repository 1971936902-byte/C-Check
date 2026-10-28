"""Stage Sharp's original, integrity-checked libvips archive for native builds."""
import argparse
import json
from pathlib import Path
import shutil

from download_verified import download


def prepare(cache, output, lock):
    if output.exists() or output.is_symlink():
        raise ValueError('libvips output directory must be new')
    if lock['filename'] != 'libvips-8.14.5-linux-x64.tar.br' or lock['libvips_version'] != '8.14.5':
        raise ValueError('unexpected pinned libvips artifact')
    if cache.is_symlink():
        raise ValueError('refusing symbolic-link cache')
    cache.mkdir(parents=True, exist_ok=True)
    archive = download(lock['url'], lock['sha512'], cache / lock['filename'],
                       algorithm='sha512', transport='curl', chunk_size=524288)
    output.mkdir(mode=0o755)
    version = output / ('v' + lock['libvips_version'])
    version.mkdir(mode=0o755)
    destination = version / lock['filename']
    with archive.open('rb') as source, destination.open('xb') as target:
        shutil.copyfileobj(source, target)
    destination.chmod(0o444)
    return {'version': lock['libvips_version'], 'sha512': lock['sha512']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(Path(__file__).with_name('sharp-libvips.lock.json').read_text())
    print(json.dumps(prepare(args.cache, args.output, lock), indent=2))


if __name__ == '__main__':
    main()
