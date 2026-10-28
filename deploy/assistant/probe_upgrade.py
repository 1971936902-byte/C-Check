"""Validate the additive assistant upgrade against isolated real MySQL, without a GPU."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--base-release', type=Path, required=True)
    parser.add_argument('--trusted-key', type=Path, required=True)
    parser.add_argument('--publisher-sha256', required=True)
    parser.add_argument('--backend-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path[:0] = [str(source / 'deploy/release'), str(source / 'deploy/release/cli')]
    from ccheck.core import verify_release
    from ccheck.image_identity import locate, inspect_image
    from validate_app_patch import mysql_migration
    base = verify_release(args.base_release, args.trusted_key, args.publisher_sha256)
    if base['version'] not in {'v0.1.0-rc.19', 'v0.1.0-rc.20'}:
        raise RuntimeError('refusing a non-Qwen2.5 assistant upgrade baseline')
    old = locate(base['images']['backend'])
    if not old:
        raise RuntimeError('signed baseline backend image is not loaded')
    new = inspect_image(args.backend_image)['Id']
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    def run(command, name, **kwargs):
        print('UPGRADE_STAGE', name, flush=True)
        with (args.output / (name + '.log')).open('wb') as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=180, **kwargs)
    report = mysql_migration(base, new, old, run, scan_progress=True, assistant_state=True)
    report.update(base_version=base['version'], base_release_sha256=base['_sha256'], backend_image=new,
                  scope='isolated MySQL additive upgrade; not a production installation')
    (args.output / 'upgrade.json').write_text(json.dumps(report, indent=2))
    print('ASSISTANT_REAL_MYSQL_UPGRADE_AND_EXISTING_DATA_PRESERVATION_OK', json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
