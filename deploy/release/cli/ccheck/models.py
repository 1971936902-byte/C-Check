import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .core import DeploymentError, atomic_json, checked_path, digest
from .inference import directory as model_directory


def model_cache_directory(site, model):
    explicit = site.get('model_paths', {}).get(model['key'])
    if explicit:
        return Path(explicit).resolve()
    if site.get('model_cache'):
        return Path(site['model_cache']).resolve() / model['cache_name']
    return None


def model_cache_file(site, model, entry):
    if site.get('delivery') is not None:
        from .delivery import model_file
        prepared = model_file(site, model, entry)
        if prepared.is_file() and prepared.stat().st_size == entry['size']:
            return prepared
    root = model_cache_directory(site, model)
    return checked_path(root, entry['path']) if root else None


def allow_model_download(site):
    from .delivery import settings
    return site.get('model_download_policy', 'online') != 'offline' and not settings(site)['offline']


def validate_offline_models(release, site):
    if allow_model_download(site):
        return
    manifest = json.loads(checked_path(release['_root'], 'models/model-manifest.json').read_text())
    for model in manifest['models']:
        cache = model_cache_directory(site, model)
        for entry in model['files']:
            installed = checked_path(model_directory(site, model['key']), entry['path'])
            candidates = [installed]
            if cache:
                candidates.append(checked_path(cache, entry['path']))
            prepared = model_cache_file(site, model, entry)
            if prepared:
                candidates.append(prepared)
            if not any(p.is_file() and p.stat().st_size == entry['size'] and digest(p) == entry['sha256']
                       for p in candidates):
                raise DeploymentError('offline model missing or differs from signed version: ' + model['key'] +
                                      '/' + entry['path'] + '; original files were not changed; check model_paths')


def obtain_file(entry, destination, cache=None, *, allow_download=True):
    destination = Path(destination)
    if destination.is_symlink():
        raise DeploymentError('symlinked artifact destination refused')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == entry["size"] and digest(destination) == entry["sha256"]:
        return "reused"
    if cache and cache.is_file() and cache.stat().st_size == entry["size"] and digest(cache) == entry["sha256"]:
        if destination.exists():
            destination.unlink()
        try:
            if not cache.stat().st_mode & 0o004:
                raise OSError('cache file needs a private permission-preserving copy')
            os.link(cache, destination)
        except OSError:
            if shutil.disk_usage(destination.parent).free < entry['size'] + 2 * 1024**3:
                raise DeploymentError('insufficient model copy space; cache was not changed')
            shutil.copy2(cache, destination)
            destination.chmod(0o644)
        return "cache"
    if not allow_download:
        raise DeploymentError('offline model unavailable; refusing network download: ' + destination.name)
    if destination.exists():
        # Only immutable, manifest-owned artifacts reach this function, never user uploads.
        destination.unlink()
    url = entry["url"]
    if urlsplit(url).scheme != "https":
        raise DeploymentError("artifact downloads must use HTTPS")
    temporary = destination.with_name(destination.name + ".part")
    if temporary.is_symlink():
        raise DeploymentError('symlinked partial download refused')
    if temporary.is_file() and temporary.stat().st_size == entry["size"]:
        if digest(temporary) == entry["sha256"]:
            temporary.chmod(0o644)
            os.replace(temporary, destination)
            return "resumed"
        temporary.unlink()
    for attempt in range(4):
        offset = temporary.stat().st_size if temporary.exists() else 0
        if offset > entry["size"]:
            temporary.unlink()
            offset = 0
        request = Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        try:
            if shutil.disk_usage(destination.parent).free < entry['size'] - offset + 2 * 1024**3:
                raise DeploymentError('insufficient artifact download space')
            with urlopen(request, timeout=90) as response:
                if urlsplit(response.url).scheme != "https":
                    raise DeploymentError("insecure artifact redirect")
                if offset and response.status != 206:
                    offset = 0
                if offset and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise DeploymentError("download resume offset mismatch")
                with temporary.open("ab" if offset else "wb") as output:
                    for block in iter(lambda: response.read(4 * 1024 * 1024), b""):
                        output.write(block)
                        if output.tell() > entry["size"]:
                            raise DeploymentError("download exceeds signed artifact size")
            if temporary.stat().st_size != entry["size"] or digest(temporary) != entry["sha256"]:
                temporary.unlink(missing_ok=True)
                raise DeploymentError("artifact hash/size mismatch")
            temporary.chmod(0o644)
            os.replace(temporary, destination)
            return "downloaded"
        except (OSError, HTTPError, DeploymentError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise DeploymentError("download retries exhausted")


def validate_model_layout(model, directory):
    directory = Path(directory)
    declared = {row['path'] for row in model['files']}
    if 'config.json' not in declared:
        raise DeploymentError('model manifest is missing config.json')
    config = json.loads((directory / 'config.json').read_text())
    if not isinstance(config, dict) or not isinstance(config.get('model_type'), str):
        raise DeploymentError('model config is missing model_type')
    # AutoTokenizer can infer Qwen/Jina BPE tokenizers from model_type and vocab/merges.
    # tokenizer_config.json is supplementary, not a universal upstream requirement.
    if 'tokenizer.json' in declared:
        tokenizer = json.loads((directory / 'tokenizer.json').read_text())
        if not isinstance(tokenizer, dict) or not tokenizer.get('model'):
            raise DeploymentError('invalid tokenizer.json model data')
    elif {'vocab.json', 'merges.txt'}.issubset(declared):
        vocab = json.loads((directory / 'vocab.json').read_text())
        if not isinstance(vocab, dict) or not vocab or not (directory / 'merges.txt').read_text().strip():
            raise DeploymentError('invalid BPE tokenizer vocabulary/merges')
    else:
        raise DeploymentError('model manifest is missing tokenizer data (tokenizer.json or vocab.json + merges.txt)')
    if model['key'] == 'qwen' and config.get('quantization_config', {}).get('quant_method') != 'fp8':
        raise DeploymentError('Qwen checkpoint is not the declared FP8 model')
    if 'model.safetensors.index.json' in declared:
        weights = json.loads((directory / 'model.safetensors.index.json').read_text()).get('weight_map')
        if not isinstance(weights, dict) or not weights or not set(weights.values()).issubset(declared):
            raise DeploymentError('model shard manifest is incomplete')
    elif 'model.safetensors' not in declared:
        raise DeploymentError('model manifest is missing safetensors weights/index')


def prepare_models(release, site, *, persist=True):
    manifest = json.loads(checked_path(release["_root"], "models/model-manifest.json").read_text())
    results = []
    root = Path(site["data_dir"]) / "models"
    for model in manifest["models"]:
        cache_root = model_cache_directory(site, model)
        destination = model_directory(site, model['key'])
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        def one(entry):
            target = checked_path(destination, entry["path"])
            cached = model_cache_file(site, model, entry)
            result = obtain_file(entry, target, cached,
                                 allow_download=allow_model_download(site))
            return {"path": entry["path"], "action": result, "sha256": entry["sha256"]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            files = list(pool.map(one, model["files"]))
        validate_model_layout(model, destination)
        results.append({"key": model["key"], "repository": model["repository"],
                        "revision": model["revision"], "files": files})
        counts = {action: sum(row['action'] == action for row in files) for action in {row['action'] for row in files}}
        if persist:
            atomic_json(Path(site['data_dir']) / 'state/models.json', results)
        print('  model', model['key'], json.dumps(counts), flush=True)
    return results


def verify_models(release, site):
    manifest = json.loads(checked_path(release['_root'], 'models/model-manifest.json').read_text())
    for model in manifest['models']:
        for entry in model['files']:
            path = checked_path(model_directory(site, model['key']), entry['path'])
            if not path.is_file() or path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
                raise DeploymentError('model revision deviated or is incomplete; rerun install to repair: ' + model['key'])
        validate_model_layout(model, model_directory(site, model['key']))
