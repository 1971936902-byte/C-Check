"""Bind image filesystem and execution config independently of OCI index wrappers."""
import hashlib
import json

from .core import run


def identity(metadata):
    defaults = {'Env': [], 'Cmd': [], 'Entrypoint': [], 'WorkingDir': '', 'User': '', 'Labels': {},
                'ExposedPorts': {}, 'Volumes': {}, 'StopSignal': '', 'StopTimeout': None,
                'Healthcheck': None, 'OnBuild': [], 'Shell': [], 'ArgsEscaped': False}
    config = dict(metadata['Config'])
    config.pop('Image', None)  # Legacy Docker parent bookkeeping, not executed image content.
    for name in ['Hostname', 'Domainname', 'AttachStdin', 'AttachStdout', 'AttachStderr', 'Tty', 'OpenStdin', 'StdinOnce', 'NetworkDisabled', 'MacAddress']:
        if config.get(name) in (None, '', False):
            config.pop(name, None)
    for name, default in defaults.items():
        if config.get(name) is None:
            config[name] = default
    payload = {'os': metadata['Os'], 'architecture': metadata['Architecture'],
               'variant': metadata.get('Variant') or '', 'rootfs': metadata['RootFS'], 'config': config}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspect_image(reference):
    return json.loads(run(['docker', 'image', 'inspect', reference]))[0]


def locate(specification):
    from .core import DeploymentError
    try:
        metadata = inspect_image(specification['id'])
        if identity(metadata) == specification['filesystem_config_sha256']:
            return metadata['Id']
    except DeploymentError:
        pass
    for image in set(run(['docker', 'image', 'ls', '--quiet', '--no-trunc']).splitlines()):
        metadata = inspect_image(image)
        if identity(metadata) == specification['filesystem_config_sha256']:
            return metadata['Id']
    return None
