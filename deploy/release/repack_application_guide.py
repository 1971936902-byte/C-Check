"""Correct an unpublished application's guide without changing validated executable artifacts."""
import argparse
import copy
from pathlib import Path
import subprocess
import sys

from repack_deployment import verify_source, signing_key, reuse_file, bootstrap, write_json
from build_release import render_install_guide
from ccheck.core import verify_release, digest

GUIDE = 'deploy/release/INSTALL-APP-PATCH.template.md'
SELF = 'deploy/release/repack_application_guide.py'


def repack(args):
    source = verify_source(args.source, committed=True)
    previous = verify_source(args.base_source, committed=True)
    base = verify_release(args.base_release, args.trusted_key)
    if base['version'] != 'v0.1.0-rc.18' or base['source']['tree_sha256'] != previous['tree_sha256']:
        raise RuntimeError('only the unpublished rc18 candidate may be repacked')
    changed = {name for name in source['files'].keys() | previous['files'].keys()
               if source['files'].get(name) != previous['files'].get(name)}
    if changed != {GUIDE, SELF}:
        raise RuntimeError('only this repacker and the installation guide may change')
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError('output must be new')
    key = signing_key(args.signing_key, [args.source, args.base_source, args.base_release, output])
    subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s',
                    str(args.source/'deploy/release/tests'), '-q'], check=True)
    output.mkdir(mode=0o700, parents=True)
    replaced = {'bootstrap.tar.gz', 'checksums.sha256', 'INSTALL-RTX5880.md'}
    for name in base['files']:
        if name not in replaced:
            reuse_file(args.base_release/name, output/name, image=name.startswith('images/'))
    public = subprocess.check_output(['openssl','pkey','-in',str(key),'-pubout'])
    if public != (output/'publisher.pem').read_bytes():
        raise RuntimeError('publisher changed')
    guide = (args.source/GUIDE).read_text(encoding='utf-8')
    (output/'INSTALL-RTX5880.md').write_text(render_install_guide(guide, base['version'], base['_publisher']), encoding='utf-8')
    evidence = {'passed':True, 'source_tree':source['tree_sha256'], 'changed_source_files':sorted(changed),
                'validated_application_source':base['source'], 'base_release_sha256':base['_sha256'],
                'application_images_cli_models_and_migrations_unchanged':True}
    write_json(output/'verify/documentation-repack.json', evidence)
    bootstrap(output)
    files = {p.relative_to(output).as_posix():digest(p) for p in output.rglob('*') if p.is_file()}
    (output/'checksums.sha256').write_text(''.join(h+'  '+name+'\n' for name,h in sorted(files.items())))
    files['checksums.sha256'] = digest(output/'checksums.sha256')
    release = copy.deepcopy(base)
    for name in ('_root','_sha256','_publisher'):
        release.pop(name, None)
    release.update(source={k:source[k] for k in ('git_sha','tree_sha256','worktree_snapshot')},
                   files=files, artifact_sizes={name:(output/name).stat().st_size for name in files},
                   bootstrap={'file':'bootstrap.tar.gz','sha256':files['bootstrap.tar.gz'],
                              'size':(output/'bootstrap.tar.gz').stat().st_size})
    release['validation']['documentation_repack'] = evidence
    write_json(output/'release.json', release)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(output/'release.json'),
                    '-out',str(output/'release.sig')],check=True)
    verify_release(output,args.trusted_key,base['_publisher'])
    for name, checksum in base['files'].items():
        if name not in replaced and files[name] != checksum:
            raise RuntimeError('reused artifact changed: '+name)
    if verify_source(args.source, committed=True) != source:
        raise RuntimeError('source changed during repack')
    print('DOCUMENTATION_ONLY_REPACK_SIGNED', output, flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','base-source','base-release','output','trusted-key','signing-key'):
        parser.add_argument('--'+name,type=Path,required=True)
    repack(parser.parse_args())
