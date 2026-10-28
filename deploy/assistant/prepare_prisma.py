"""Prepare verified pinned Prisma engines for offline provider installation."""
import argparse
import gzip
import json
from pathlib import Path

from download_verified import digest, download


def prepare(cache, output, lock):
    if output.exists() or output.is_symlink():
        raise ValueError('engine output directory must be new')
    if cache.is_symlink():
        raise ValueError('refusing symbolic-link cache')
    cache.mkdir(parents=True, exist_ok=True)
    engines = lock['engines']
    if {engine['name'] for engine in engines} != {'libquery_engine.so.node', 'schema-engine'} or len(engines) != 2:
        raise ValueError('unexpected engine set')
    archives = []
    for engine in engines:
        if Path(engine['archive']).name != engine['archive'] or '/' in engine['archive'] or '\\' in engine['archive']:
            raise ValueError('invalid engine archive name')
        url = ('https://binaries.prisma.sh/all_commits/' + lock['commit'] + '/'
               + lock['platform'] + '/' + engine['name'] + '.gz')
        archive = download(url, engine['sha256'], cache / engine['archive'],
                           chunk_size=524288, transport='curl')
        archives.append((engine, archive))
    output.mkdir(mode=0o755)
    evidence = {}
    for engine, archive in archives:
        destination = output / engine['name']
        try:
            with gzip.open(archive, 'rb') as source, destination.open('xb') as target:
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError('engine exceeds extraction limit')
                    target.write(chunk)
            if size == 0:
                raise ValueError('empty engine')
            destination.chmod(0o555)
            evidence[engine['name']] = {'archive_sha256': engine['sha256'], 'binary_sha256': digest(destination)}
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(Path(__file__).with_name('prisma-engines.lock.json').read_text())
    evidence = prepare(args.cache, args.output, lock)
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
