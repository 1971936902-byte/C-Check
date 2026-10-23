import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time


class DeploymentError(RuntimeError):
    pass


def gpu_memory_fraction(profile):
    fractions = {'a100-40g-fp8': 0.94, 'ada-48g-fp8': 0.88}
    if profile not in fractions:
        raise DeploymentError('select a supported hardware profile')
    return fractions[profile]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ccheck-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checked_path(root, relative):
    relative = PurePosixPath(relative)
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or not relative.parts:
        raise DeploymentError("unsafe release path")
    root = Path(root).resolve()
    target = root.joinpath(*relative.parts)
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise DeploymentError("symbolic link is not allowed in a managed artifact path")
    if not target.resolve().is_relative_to(root):
        raise DeploymentError("artifact path escapes release")
    return target


def run(command, *, timeout=180, capture=True, cwd=None, env=None, input_data=None):
    result = subprocess.run(command, cwd=cwd, env=env, input=input_data,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout)
    if result.returncode:
        # Commands may print secrets in diagnostics; retain only the exit code here.
        raise DeploymentError("operation failed: " + str(command[0]) + " (exit " + str(result.returncode) + ")")
    return result.stdout.decode("utf-8", "replace") if capture else ""


def verify_release(root, trusted_key, expected_fingerprint=None):
    root = Path(root).resolve()
    key = Path(trusted_key).resolve()
    if not key.is_file():
        raise DeploymentError("an independently trusted publisher public key is required")
    if os.name == 'posix' and (key.stat().st_mode & 0o022 or key.stat().st_uid not in {0, os.getuid()}):
        raise DeploymentError('trusted publisher key must not be writable by other users')
    version = run(['openssl', 'version'])
    match = re.search(r'OpenSSL\s+(\d+)\.', version)
    if not match or int(match.group(1)) < 3:
        raise DeploymentError('OpenSSL 3 or newer is required for Ed25519 verification')
    fingerprint = digest(key)
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise DeploymentError("publisher public key differs from installed trust anchor")
    if (root / 'release.json').stat().st_size > 4 * 1024 * 1024 or (root / 'release.sig').stat().st_size != 64:
        raise DeploymentError('unsupported release metadata/signature size; Ed25519 is required')
    run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",
         "-in", str(root / "release.json"), "-sigfile", str(root / "release.sig")])
    release = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if release.get("format") != 1 or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", release.get("version", "")):
        raise DeploymentError("unsupported release manifest or version")
    if release.get("tag") != "release-" + release["version"]:
        raise DeploymentError("release tag/version mismatch")
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink() or path.name == '.git':
            raise DeploymentError('symlink/Git metadata in release payload')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise DeploymentError('special file in release payload')
    if actual - set(release['files']) - {'release.json', 'release.sig', '.fetch-owner.json'}:
        raise DeploymentError('unlisted artifact in release payload; refusing possible source/key leakage')
    for name, expected in release["files"].items():
        path = checked_path(root, name)
        if not path.is_file() or digest(path) != expected:
            raise DeploymentError("release artifact checksum mismatch: " + name)
    required = {"core-policy.json", "models/model-manifest.json", "cli/ccheck.pyz"}
    if not required.issubset(release["files"]):
        raise DeploymentError("release is missing mandatory signed artifacts")
    release["_root"] = str(root)
    release["_sha256"] = digest(root / "release.json")
    release["_publisher"] = fingerprint
    return release


def load_site(path):
    try:
        import yaml
        class UniqueLoader(yaml.SafeLoader):
            def construct_mapping(self, node, deep=False):
                result = {}
                for key, value in self.construct_pairs(node, deep=deep):
                    if not isinstance(key, str) or key in result:
                        raise DeploymentError('configuration keys must be unique strings')
                    result[key] = value
                return result
        try:
            site = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueLoader)
        except yaml.YAMLError as exc:
            raise DeploymentError('invalid YAML configuration; no resources were changed') from exc
    except ImportError:
        site = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(site, dict) or not re.fullmatch(r"[a-z][a-z0-9-]{2,35}", site.get("instance", "")):
        raise DeploymentError("instance must be a 3-36 character lower-case project name")
    allowed = {"instance", "data_dir", "profile", "gpu", "web", "model_cache", "model_paths",
               "model_download_policy", "license_mode", "engine", "runtime_source", "backup", "gpu_access_mode", "network_subnets", "performance"}
    if set(site) - allowed:
        raise DeploymentError("unknown site configuration keys")
    if site.get("engine", "docker") != "docker" or site.get("runtime_source", "image") not in {"image", "build"}:
        raise DeploymentError("select Docker and an explicit image/build runtime source")
    site.setdefault("engine", "docker")
    site.setdefault("runtime_source", "image")
    from .gpu import access_mode
    access_mode(site)
    from .networks import subnets
    subnets(site)
    from .acceleration import settings
    settings(site)
    backup = site.get('backup', {'keep': 3})
    if not isinstance(backup, dict) or set(backup) != {'keep'} or type(backup['keep']) is not int or not 1 <= backup['keep'] <= 30:
        raise DeploymentError('backup.keep must be an integer between 1 and 30')
    site['backup'] = backup
    raw = Path(site["data_dir"]).expanduser()
    if not raw.is_absolute() or any(character in str(raw) for character in [',', '\n', '\r', '\x00']):
        raise DeploymentError('data_dir must be an absolute path without control characters or commas')
    if len(raw.parts) < 3 or raw == Path.home() or raw == Path("/tmp"):
        raise DeploymentError("data_dir must be a dedicated project subdirectory")
    if any(p.is_symlink() for p in [raw, *raw.parents]):
        raise DeploymentError("symlinked installation paths are not supported")
    site["data_dir"] = str(raw.resolve())
    if site.get('model_cache') and not Path(site['model_cache']).expanduser().is_absolute():
        raise DeploymentError('model_cache must be an absolute read-only cache path')
    paths = site.get('model_paths', {})
    if not isinstance(paths, dict) or set(paths) - {'qwen', 'jina'}:
        raise DeploymentError('model_paths accepts only qwen and jina absolute directories')
    for value in paths.values():
        if not isinstance(value, str) or not Path(value).is_absolute() or any(c in value for c in '\x00\n\r'):
            raise DeploymentError('model_paths values must be absolute read-only directories')
    if site.get('model_download_policy', 'online') not in {'online', 'offline'}:
        raise DeploymentError('model_download_policy must be online or offline')
    gpu_memory_fraction(site.get('profile'))
    if not re.fullmatch(r"\d+|GPU-[a-fA-F0-9-]+", str(site.get("gpu", ""))):
        raise DeploymentError("select exactly one GPU index or UUID")
    web = site["web"]
    if set(web) - {"bind", "port", "public_url", "exposure"}:
        raise DeploymentError("unknown web configuration keys")
    import ipaddress
    address = ipaddress.ip_address(web["bind"])
    if address.version != 4 or type(web.get('port')) is not int or not 1024 <= web["port"] <= 65535:
        raise DeploymentError("V1 requires IPv4 and a non-privileged Web port")
    if web.get("exposure") not in {"private", "lan", "gateway", "direct"}:
        raise DeploymentError("invalid exposure mode")
    if web["exposure"] == "private" and not address.is_loopback:
        raise DeploymentError("private mode must bind loopback")
    from urllib.parse import urlsplit
    origin = urlsplit(web["public_url"])
    if origin.scheme not in {"http", "https"} or not origin.netloc or origin.path not in {"", "/"} or origin.username or origin.query or origin.fragment:
        raise DeploymentError("public_url must be an HTTP(S) site origin")
    if web["exposure"] == "direct" and origin.scheme != "https":
        raise DeploymentError("direct public mode requires an HTTPS gateway; use gateway mode for existing NAT evaluation")
    if web['exposure'] == 'direct':
        raise DeploymentError('direct TLS termination is outside the V1 support matrix; provision an HTTPS gateway and select gateway mode')
    site["web"]["public_url"] = web["public_url"].rstrip("/")
    if site.get("license_mode") not in {"evaluation", "authorized"}:
        raise DeploymentError("explicit model license mode is required; review the release model notices")
    return site


@contextlib.contextmanager
def installation_lock(root, *, create=False):
    root = Path(root)
    marker = root / "state/owner.json"
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise DeploymentError("refusing to take over a nonempty unowned directory")
    if root.exists() and not marker.exists() and root.stat().st_uid != os.getuid():
        raise DeploymentError("new installation directory must belong to the invoking user")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise DeploymentError("installation does not exist")
    root.chmod(0o700)
    import fcntl
    with checked_path(root, '.install.lock').open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("another operation owns the installation lock") from exc
        yield


def stage(root, name, operation):
    atomic_json(Path(root) / "state/operation.json", {"stage": name, "status": "running", "time": time.time()})
    print("[" + name + "]", flush=True)
    try:
        result = operation()
    except Exception:
        atomic_json(Path(root) / "state/operation.json", {"stage": name, "status": "failed", "time": time.time()})
        raise
    atomic_json(Path(root) / "state/operation.json", {"stage": name, "status": "completed", "time": time.time()})
    return result
