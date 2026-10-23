"""Scheme two: small signed release, immutable images/models fetched on demand."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote

sys.dont_write_bytecode = True
from repack_deployment import bootstrap, signing_key, verify_source, source_files, tooling_path, write_json
from build_release import render_install_guide
from ccheck.core import digest, verify_release
from ccheck.fetch import asset_prefix
from ccheck.delivery import validate_release


EXTRA_INPUTS = {'deploy/release/build_online_release.py', 'deploy/release/site-online.example.json',
                'deploy/release/ONLINE-INSTALL.template.md'}


def verify_inputs(base, source, previous_root):
    previous = verify_source(previous_root)
    if any(previous[field] != base['source'][field] for field in ['git_sha', 'tree_sha256']):
        raise RuntimeError('original frozen source differs from the signed baseline')
    changed = sorted(name for name in previous['files'].keys() | source['files'].keys()
                     if previous['files'].get(name) != source['files'].get(name))
    if any(not tooling_path(name) and name not in EXTRA_INPUTS for name in changed):
        raise RuntimeError('online repack may change only delivery tools; application/model/image changes require full build gates')
    return changed


def online_images(base, base_url):
    images = copy.deepcopy(base['images'])
    for name, spec in images.items():
        parts = spec.pop('archive_parts', None)
        if not parts or spec.pop('archive', None):
            raise RuntimeError('baseline must supply signed split archives for every runtime image')
        spec['remote_parts'] = [{'name': Path(part).name,
            'url': asset_prefix(base, part, base_url) + quote(Path(part).name, safe=''),
            'sha256': base['files'][part], 'size': base['artifact_sizes'][part]} for part in parts]
    return images


def build(args):
    root, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        raise RuntimeError('online output must be a new directory; offline packages are never overwritten')
    key = signing_key(args.signing_key, [root, args.base_release, output, args.yaml_package])
    base = verify_release(args.base_release, args.trusted_key)
    source = verify_source(root, committed=True)
    changed = verify_inputs(base, source, args.base_source)
    report = json.loads(args.validation_report.read_text())
    if report.get('passed') is not True or report.get('source_tree') != source['tree_sha256']:
        raise RuntimeError('passing tool report must match this exact frozen source')
    if args.version != 'v0.2.0-rc.1':
        raise RuntimeError('this first online builder requires v0.2.0-rc.1')
    output.mkdir(mode=0o700, parents=True)
    omitted = {'release.json', 'release.sig', 'bootstrap.tar.gz', 'checksums.sha256',
               'cli/ccheck.pyz', 'site-rtx5880.example.json', 'INSTALL-RTX5880.md', 'README.md',
               base['core_wheel']['file'], 'verify/deployment-tooling.json'}
    for name in base['files']:
        if name in omitted or name.startswith(('images/', 'runtime/')):
            continue
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.base_release / name, target)
    subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(output/'publisher.pem')], check=True)
    if digest(output/'publisher.pem') != base['_publisher']:
        raise RuntimeError('online and offline publisher identity must remain the same')
    for name in ['ccheck', 'install.sh']:
        shutil.copy2(root/'deploy/release'/name, output/name)
        (output/name).chmod(0o755)
    shutil.copy2(output/'ccheck', output/'ccheckctl')
    (output/'ccheckctl').chmod(0o755)
    shutil.copy2(root/'deploy/release/site-online.example.json', output/'site-online.example.json')
    subprocess.run([sys.executable, '-B', str(root/'deploy/release/build_cli.py'), '--output',
                    str(output/'cli/ccheck.pyz'), '--yaml-package', str(args.yaml_package)], check=True)
    template=(root/'deploy/release/ONLINE-INSTALL.template.md').read_text(encoding='utf-8')
    (output/'INSTALL-ONLINE.md').write_text(render_install_guide(template,args.version,digest(output/'publisher.pem')),encoding='utf-8')
    shutil.copy2(args.validation_report,output/'verify/online-tool-tests.json')
    images=online_images(base,args.asset_base_url)
    inventory=json.loads((output/'licenses/python-components.json').read_text())
    lock={'format':1,'version':args.version,'platform':'linux/amd64','manager':'Docker Compose + OCI digests + hash-locked Python build inputs',
        'images':images,'python_environments':{},'models':json.loads((output/'models/model-manifest.json').read_text()),
        'host_installation':{'automatic_driver_docker_toolkit_install':False,'host_pip_install':False},
        'baseline':{'version':base['version'],'release_sha256':base['_sha256']}}
    for group in ['backend','qwen','embedding','compiler']:
        name='locks/'+group+'.lock'
        lock['python_environments'][group]={'lock':name,'sha256':digest(output/name),
            'packages':[{key:row[key] for key in ['name','version','wheel_sha256']}
                        for row in inventory if row['environment']==group]}
    write_json(output/'components.lock.json',lock)
    (output/'README.md').write_text('# C-Check '+args.version+' online delivery\n\n'
        'No image archives, model weights or host Python installations. See INSTALL-ONLINE.md. '
        'Missing images and models are downloaded using signed digests and SHA256 locks. '
        'Offline release-v0.1.0-rc.16 remains available and unchanged.\n',encoding='utf-8')
    bootstrap(output)
    files={p.relative_to(output).as_posix():digest(p) for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'checksums.sha256').write_text(''.join(checksum+'  '+name+'\n' for name,checksum in sorted(files.items())))
    files['checksums.sha256']=digest(output/'checksums.sha256')
    release={key:copy.deepcopy(base[key]) for key in ['format','platform','schema_epoch','schema_files','profiles','runtime_launchers']}
    release.update(version=args.version,tag='release-'+args.version,images=images,runtime_recipe=None,
        source={key:source[key] for key in ['git_sha','tree_sha256','worktree_snapshot']},files=files,
        artifact_sizes={name:(output/name).stat().st_size for name in files},
        bootstrap={'file':'bootstrap.tar.gz','sha256':files['bootstrap.tar.gz'],'size':(output/'bootstrap.tar.gz').stat().st_size},
        distribution={'kind':'online','base_version':base['version'],'base_release_sha256':base['_sha256']},
        core_binary={'image':'backend','abi':base['core_wheel']['abi'],'modules':base['core_wheel']['modules']},
        components_lock={'file':'components.lock.json','sha256':files['components.lock.json']},
        artifact_reuse={'images_and_models_unchanged':True,'changed_source_files':changed},
        validation={'tool_tests':{'passed':True,'report':'verify/online-tool-tests.json'},
                    'inherited_runtime':base['validation'],'full_install_tested_at_build_time':False})
    validate_release(release)
    write_json(output/'release.json',release)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(output/'release.json'),
                    '-out',str(output/'release.sig')],check=True)
    verify_release(output,args.trusted_key)
    if verify_source(root,committed=True)!=source:
        raise RuntimeError('frozen source changed while packaging')
    print('SIGNED_ONLINE_RELEASE',output,'BOOTSTRAP_BYTES',(output/'bootstrap.tar.gz').stat().st_size)
    return release


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['base-release','base-source','trusted-key','source','output','signing-key','yaml-package','validation-report']:
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--asset-base-url',default='https://github.com/1971936902-byte/C-Check/releases/download')
    build(parser.parse_args())


if __name__=='__main__':
    main()
