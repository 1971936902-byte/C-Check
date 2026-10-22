import json
from pathlib import Path
import re
import tarfile
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from .core import DeploymentError, atomic_json, checked_path, run, verify_release
from .models import obtain_file


def fetch_release(base_url, version, destination, trusted_key):
    origin = urlsplit(base_url)
    if origin.scheme != 'https' or not origin.netloc or origin.username or origin.query or origin.fragment:
        raise DeploymentError('release source must be an HTTPS base URL without embedded credentials')
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.\d+)?', version):
        raise DeploymentError('invalid requested release version')
    root = Path(destination).expanduser().absolute()
    if any(path.is_symlink() for path in [root, *root.parents]):
        raise DeploymentError('symlinked release destination is not supported')
    root = root / ('release-' + version)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = root / '.fetch-owner.json'
    identity = {'base_url': base_url.rstrip('/'), 'version': version}
    if owner.exists():
        if json.loads(owner.read_text()) != identity:
            raise DeploymentError('refusing to overwrite a different release download')
    elif any(root.iterdir()):
        raise DeploymentError('release download target is not empty or owned')
    atomic_json(owner, identity)
    prefix = base_url.rstrip('/') + '/release-' + version + '/'
    for filename in ['release.json', 'release.sig']:
        with urlopen(prefix + filename, timeout=60) as response:
            if urlsplit(response.url).scheme != 'https':
                raise DeploymentError('insecure release redirect')
            content = response.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise DeploymentError('release metadata exceeds limit')
        (root / filename).write_bytes(content)
    run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(trusted_key), '-rawin',
         '-in', str(root / 'release.json'), '-sigfile', str(root / 'release.sig')])
    manifest = json.loads((root / 'release.json').read_text())
    if manifest.get('version') != version or manifest.get('tag') != 'release-' + version:
        raise DeploymentError('signed release does not match requested version')
    if len(manifest['files']) > 10000:
        raise DeploymentError('too many release assets')
    bootstrap = manifest['bootstrap']
    target = checked_path(root, bootstrap['file'])
    obtain_file({'size': bootstrap['size'], 'sha256': bootstrap['sha256'],
                 'url': prefix + quote(bootstrap['file'], safe='')}, target)
    with tarfile.open(target, 'r:gz') as archive:
        for member in archive:
            if member.isdir():
                checked_path(root, member.name).mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.name not in manifest['files']:
                raise DeploymentError('unexpected entry in signed bootstrap archive')
            path = checked_path(root, member.name)
            if member.size != manifest['artifact_sizes'][member.name]:
                raise DeploymentError('bootstrap member size mismatch')
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open('wb') as output:
                import shutil
                shutil.copyfileobj(source, output)
            path.chmod(0o755 if member.name in {'ccheck', 'install.sh', 'ccheckctl'} else 0o600)
    for name, checksum in manifest['files'].items():
        target = checked_path(root, name)
        obtain_file({'size': manifest['artifact_sizes'][name], 'sha256': checksum,
                     'url': prefix + quote(Path(name).name, safe='')}, target)
    release = verify_release(root, trusted_key)
    print('FETCHED', release['version'], root)
    return root
