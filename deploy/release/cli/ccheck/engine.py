import json
import hashlib
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import time

from .core import DeploymentError, atomic_json, checked_path, digest, gpu_memory_fraction, run
from .gpu import access_mode, cdi_device, check_identity, configure, NVML_PROBE, probe, validate_inspection
from .networks import DEFAULT_SUBNETS, specifications, subnets
from . import acceleration
from . import delivery
from . import inference
from . import assistant


def project_name(site):
    suffix = hashlib.sha256(site['data_dir'].encode()).hexdigest()[:16]
    return 'ccheck-' + site['instance'] + '-' + suffix


def doctor(site):
    preflight(site)
    probe = Path(site['data_dir'])
    while not probe.exists():
        probe = probe.parent
    return {
        'prerequisites_passed': True, 'profile': site['profile'], 'gpu': str(site['gpu']),
        'gpu_access_mode': access_mode(site),
        'cdi_device': cdi_device(site) if access_mode(site) == 'cdi' else None,
        'docker': run(['docker', 'version', '--format', '{{.Server.Version}}']).strip(),
        'compose': run(['docker', 'compose', 'version', '--short']).strip(),
        'openssl': run(['openssl', 'version']).strip(),
        'gpu_details': run(['nvidia-smi', '-i', str(site['gpu']),
                            '--query-gpu=name,uuid,driver_version,memory.free,memory.total',
                            '--format=csv,noheader']).strip(),
        'data_filesystem_free_bytes': shutil.disk_usage(probe).free,
        'container_cuda_and_real_inference_tested': False,
        'note': 'Read-only prerequisite check; no downloads, GPU allocation or driver changes. Full install acceptance is still required.',
    }


def gateway_snippet(site):
    bind = site['web']['bind']
    upstream = '127.0.0.1' if bind == '0.0.0.0' else bind
    return (
        '# Review before adding to an existing same-host HTTPS gateway server block.\n'
        '# Remote gateways require a reachable LAN bind; DNS/TLS/NAT are managed separately.\n'
        'location / {\n'
        f'    proxy_pass http://{upstream}:{site["web"]["port"]};\n'
        '    proxy_http_version 1.1;\n'
        '    proxy_set_header Host $host;\n'
        '    proxy_set_header X-Real-IP $remote_addr;\n'
        '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
        '    proxy_set_header X-Forwarded-Proto $scheme;\n'
        '    client_max_body_size 100m;\n'
        '    proxy_read_timeout 1800s;\n'
        '}\n'
    )


def preflight(site, release=None):
    if release is not None:
        inference.configure(site, release)
        assistant.validate_release(site, release)
        delivery.validate_release(release)
        if delivery.is_online(release):
            site.setdefault('delivery', {})
        if delivery.is_online(release) and site.get('runtime_source') == 'build':
            raise DeploymentError('online release uses pinned runtime images, not deployment-time source builds')
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise DeploymentError("this release supports Linux/amd64 only")
    for name in ("docker", "openssl", "nvidia-smi"):
        if shutil.which(name) is None:
            raise DeploymentError("administrator must prepare prerequisite: " + name)
    run(["docker", "info"])
    run(["docker", "compose", "version"])
    if access_mode(site) == 'cdi':
        if shutil.which('nvidia-ctk') is None:
            raise DeploymentError('CDI mode requires administrator-prepared nvidia-ctk and CDI specifications')
        available = run(['nvidia-ctk', 'cdi', 'list'])
        if cdi_device(site) not in {line.strip() for line in available.splitlines()}:
            raise DeploymentError('selected GPU is absent from CDI specifications; no driver/daemon changes were attempted')
    gpu_details = run(["nvidia-smi", "-i", str(site["gpu"]), "--query-gpu=uuid,memory.total,driver_version", "--format=csv,noheader"])
    if acceleration.gpu_embedding(site):
        import csv
        rows = list(csv.reader(gpu_details.strip().splitlines()))
        if len(rows) != 1 or len(rows[0]) != 3 or float(rows[0][1].strip().split()[0]) < 46 * 1024:
            raise DeploymentError('shared GPU embedding requires a single 48GB-class GPU; no models were stopped')
    probe = Path(site["data_dir"])
    while not probe.exists():
        probe = probe.parent
    minimum = 6 if release is not None and delivery.is_online(release) else 12
    if shutil.disk_usage(probe).free < minimum * 1024**3:
        raise DeploymentError(f'at least {minimum} GiB free is required before artifact/model preparation')
    if release is not None:
        acceleration.validate_release(site, release)
        from .models import model_cache_directory, model_cache_file, validate_offline_models
        validate_offline_models(release, site)
        cached = Path(site['data_dir']) / 'releases' / release['version']
        needed_data = 0 if cached.exists() else sum(release.get('artifact_sizes', {}).values())
        models = json.loads(checked_path(release['_root'], 'models/model-manifest.json').read_text())['models']
        for model in models:
            cache = model_cache_directory(site, model)
            for entry in model['files']:
                target = inference.directory(site, model['key']) / entry['path']
                if target.is_file() and target.stat().st_size == entry['size']:
                    continue
                source = model_cache_file(site, model, entry)
                if (source and source.is_file() and source.stat().st_size == entry['size']
                        and source.stat().st_dev == probe.stat().st_dev and source.stat().st_mode & 0o004):
                    continue
                needed_data += entry['size']
        needed_docker = 0
        for name, spec in release['images'].items():
            try:
                run(['docker', 'image', 'inspect', spec['id']])
            except DeploymentError:
                if spec.get('filesystem_config_sha256'):
                    from .image_identity import locate
                    if locate(spec):
                        continue
                if not (name == 'qwen' and site.get('runtime_source') == 'build'):
                    needed_docker += int(spec.get('size_bytes', 0)) * 2
                    if delivery.is_online(release):
                        needed_data += sum(part['size'] for part in spec['remote_parts'])
        if site.get('runtime_source') == 'build':
            from .runtime import cached_runtime_image
            if cached_runtime_image(release, site) is None:
                needed_docker += int(release['images']['qwen'].get('size_bytes', 0)) * 2
                needed_data += sum(row['size'] for row in json.loads(checked_path(release['_root'], 'runtime/qwen/wheels.json').read_text()))
        docker_root = Path(run(['docker', 'info', '--format', '{{.DockerRootDir}}']).strip())
        safety = 6 * 1024**3
        if probe.stat().st_dev == docker_root.stat().st_dev:
            if shutil.disk_usage(probe).free < needed_data + needed_docker + safety:
                raise DeploymentError('insufficient shared filesystem space for release/model/image peak budget')
        elif shutil.disk_usage(probe).free < needed_data + safety or shutil.disk_usage(docker_root).free < needed_docker + safety:
            raise DeploymentError('insufficient data or Docker filesystem space')
    if not (Path(site["data_dir"]) / "state/deployment.json").exists():
        with socket.socket() as sock:
            try:
                sock.bind((site["web"]["bind"], int(site["web"]["port"])))
            except OSError as exc:
                raise DeploymentError("Web bind address/port is unavailable") from exc


def prepare_data(site, release):
    site['network_subnets'] = {name: cidr for name, cidr in subnets(site).items() if name in DEFAULT_SUBNETS}
    root = Path(site["data_dir"])
    owner = checked_path(root, "state/owner.json")
    expected = {"instance": site["instance"], "root": str(root)}
    previous = json.loads(owner.read_text()) if owner.exists() else None
    if previous and any(previous.get(key) != value for key, value in expected.items()):
        raise DeploymentError("installation ownership does not match site configuration")
    uid = previous['uid'] if previous else (os.getuid() if os.getuid() else 10001)
    gid = previous['gid'] if previous else (os.getgid() if os.getuid() else 10001)
    if uid < 1000 or gid < 1000:
        raise DeploymentError('managed application UID/GID must be non-root')
    atomic_json(owner, dict(expected, uid=uid, gid=gid))
    for name in ("config", "secrets", "models", "state", "releases", "backups", "data"):
        checked_path(root, name).mkdir(mode=0o700, exist_ok=True)
    for name in ("uploads", "cache", "control", "qdrant"):
        path = checked_path(root, 'data/' + name)
        path.mkdir(mode=0o700, exist_ok=True)
        if os.getuid() == 0:
            os.chown(path, uid, gid)
    for name in ("mysql", "redis"):
        checked_path(root, 'data/' + name).mkdir(mode=0o700, exist_ok=True)
    secret_root = root / "secrets"
    initialized = (root / 'state/deployment.json').exists() or any((root / 'data/mysql').iterdir())
    for name in ("mysql_password", "mysql_root_password", "jwt_secret", "admin_password", "vllm_api_key", "embedding_api_key"):
        path = checked_path(root, 'secrets/' + name)
        if not path.exists():
            if initialized:
                raise DeploymentError('initialized installation is missing a secret; restore the original, do not reset credentials')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_hex(24))
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise DeploymentError('secret files must be regular owner-only files: ' + name)
        if os.getuid() == 0:
            os.chown(path, uid, gid)
    database_file = checked_path(root, 'secrets/database_url')
    if not database_file.exists():
        password = (secret_root / "mysql_password").read_text()
        database_file.write_text("mysql+pymysql://c_check:" + password + "@mysql:3306/c_check")
        database_file.chmod(0o600)
        if os.getuid() == 0:
            os.chown(database_file, uid, gid)
    if not database_file.is_file() or database_file.stat().st_mode & 0o077:
        raise DeploymentError('database URL secret must be a regular owner-only file')
    atomic_json(root / "config/site.json", site)
    acceleration.prepare(site)
    assistant.prepare(site, uid, gid)
    from .remote_terminal import prepare_public_config
    prepare_public_config(site)
    return uid, gid


def prepare_images(release, skip=(), *, site=None, cache_locked=False):
    delivery.validate_release(release)
    if delivery.is_online(release) and site is None:
        raise DeploymentError('online image preparation requires site download configuration')
    if delivery.is_online(release) and not cache_locked:
        with delivery.cache_lock(site):
            return prepare_images(release, skip=skip, site=site, cache_locked=True)
    from .image_identity import locate
    images = {}
    for name, specification in release["images"].items():
        if name in skip:
            continue
        expected = specification["id"]
        imported = False
        actual = locate(specification)
        if actual is None:
            imported = True
            if delivery.is_online(release):
                delivery.import_image(specification, site)
            elif specification.get("archive_parts"):
                import subprocess
                with __import__('tempfile').TemporaryFile() as log:
                    process = subprocess.Popen(['docker', 'image', 'load'], stdin=subprocess.PIPE, stdout=log, stderr=log)
                    try:
                        for part in specification['archive_parts']:
                            with checked_path(release['_root'], part).open('rb') as stream:
                                shutil.copyfileobj(stream, process.stdin, length=4 * 1024 * 1024)
                        process.stdin.close()
                        if process.wait(timeout=3600):
                            raise DeploymentError('split image archive import failed: ' + name)
                    except Exception:
                        process.kill()
                        process.wait()
                        raise
            elif specification.get("archive"):
                archive = checked_path(release["_root"], specification["archive"])
                run(["docker", "image", "load", "--input", str(archive)], timeout=3600)
            elif specification.get("reference") and "@sha256:" in specification["reference"]:
                run(["docker", "pull", specification["reference"]], timeout=3600)
            else:
                raise DeploymentError("image has no immutable retrieval source: " + name)
            actual = locate(specification)
        if actual is None:
            raise DeploymentError("loaded image filesystem/config identity does not match signed release")
        images[name] = actual
        print('  image', name, 'imported' if imported else 'verified-cache', flush=True)
    return images


def render_compose(site, images, uid, gid):
    root = Path(site["data_dir"])
    memory_fraction = acceleration.memory_fraction(site)
    secret_names = ("mysql_password", "mysql_root_password", "database_url", "jwt_secret", "admin_password", "vllm_api_key", "embedding_api_key")
    env = {
        "DATABASE_URL_FILE": "/run/secrets/database_url", "REDIS_URL": "redis://redis:6379/0",
        "JWT_SECRET_FILE": "/run/secrets/jwt_secret", "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_FILE": "/run/secrets/admin_password", "ALLOW_INSECURE_DEFAULTS": "false",
        "STORAGE_PATH": "/data/uploads", "MOCK_MODEL_ENABLED": "false", "MODEL_DEPLOYMENT_ENABLED": "false",
        "MODEL_CONTEXT_WINDOW": "8192", "MODEL_MAX_INPUT_TOKENS": "5500", "MODEL_MAX_TOKENS": "2048",
        "CANDIDATE_MODEL_MAX_TOKENS": "2048", "MODEL_CHUNK_CONCURRENCY": "1", "MODEL_STRUCTURED_OUTPUTS_ENABLED": "true",
        "CANDIDATE_FORMAT_MODEL_ENABLED": "true", "CANDIDATE_FORMAT_BATCH_SIZE": "10",
        "RAG_ENABLED": "true", "RAG_SUPPLEMENTAL_PERSIST_ENABLED": "false",
        "RAG_EMBEDDING_BACKEND": "openai-compatible", "RAG_EMBEDDING_BASE_URL": "http://embedding:8002",
        "RAG_EMBEDDING_API_KEY_FILE": "/run/secrets/embedding_api_key", "RAG_EMBEDDING_MODEL": "jina-code-embeddings-1.5b",
        "RAG_EMBEDDING_DIMENSION": "1536", "RAG_EMBEDDING_BATCH_SIZE": "1", "RAG_EMBEDDING_TIMEOUT_SECONDS": "300",
        "RAG_EMBEDDING_ALLOW_HASH_FALLBACK": "false", "RAG_EMBEDDING_MAX_CHARS": "16000",
        "RAG_EMBEDDING_QUERY_PREFIX": "Find the most relevant code snippet given the following query:\n",
        "RAG_EMBEDDING_PASSAGE_PREFIX": "Candidate code snippet:\n", "RAG_QDRANT_URL": "http://qdrant:6333",
        "RAG_QDRANT_COLLECTION": "c_check_jina15b_1536", "RAG_PARSER_REQUIRE_LIBCLANG": "true",
        "RAG_PARSER_REQUIRE_TREE_SITTER": "true", "CLANG_STATIC_ANALYSIS_ENABLED": "false",
        "VLLM_API_KEY_FILE": "/run/secrets/vllm_api_key", "CORS_ORIGINS": json.dumps([site["web"]["public_url"]]),
        "DEPLOYMENT_MAINTENANCE_FILE": "/data/control/maintenance", "XDG_CACHE_HOME": "/cache",
        "USER": "ccheck", "LOGNAME": "ccheck", "HOME": "/cache",
        "LD_LIBRARY_PATH": "/usr/lib/llvm-14/lib", "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "4",
    }
    if inference.served_name(site) != inference.LEGACY:
        env['CCHECK_REVIEW_MODEL'] = inference.served_name(site)
    def bind(source, target, readonly=False):
        return {"type": "bind", "source": str(source), "target": target, "read_only": readonly,
                "bind": {"create_host_path": False}}
    def service(image, memory, cpus, networks, *, user=True):
        result = {"image": images[image], "pull_policy": "never", "restart": "unless-stopped",
                  "read_only": True, "tmpfs": ["/tmp:size=536870912,mode=1777"],
                  "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                  "pids_limit": 512, "mem_limit": memory, "cpus": cpus, "networks": networks,
                  "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
        if user:
            result["user"] = f"{uid}:{gid}"
        return result
    def health(test, interval="15s", start="30s"):
        return {"test": test, "interval": interval, "timeout": "10s", "retries": 30, "start_period": start}
    app = service("backend", "2g", 2, ["data", "inference", "edge"])
    app.update(environment=env, secrets=["database_url", "jwt_secret", "admin_password", "vllm_api_key", "embedding_api_key"],
               volumes=[bind(root / "data/uploads", "/data/uploads"), bind(root / "data/cache", "/cache"),
                        bind(root / "data/control", "/data/control", True)], working_dir="/app/backend")
    api = dict(app)
    api["command"] = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    api['environment'] = dict(env, NVIDIA_DRIVER_CAPABILITIES='utility')
    configure(api, site)
    api["healthcheck"] = health(["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/docs',timeout=5)"])
    worker = dict(app)
    worker["mem_limit"] = "4g"
    worker["command"] = ["celery", "-A", "app.worker.celery_app", "worker", "--loglevel=INFO", "--concurrency=1", "--prefetch-multiplier=1"]
    worker["healthcheck"] = health(["CMD-SHELL", "celery -A app.worker.celery_app inspect ping --destination celery@$$HOSTNAME --timeout 5 | grep -q pong"], "30s")
    init = dict(app)
    init.update(restart="no", command=["python", "-m", "app.deployment", "migrate"], profiles=["tools"])
    mysql = service("mysql", "3g", 2, ["data"], user=False)
    mysql.update(read_only=False, cap_drop=[], cap_add=["CHOWN", "SETGID", "SETUID", "DAC_OVERRIDE", "FOWNER"],
                 environment={"MYSQL_DATABASE": "c_check", "MYSQL_USER": "c_check",
                              "MYSQL_PASSWORD_FILE": "/run/secrets/mysql_password", "MYSQL_ROOT_PASSWORD_FILE": "/run/secrets/mysql_root_password"},
                 secrets=["mysql_password", "mysql_root_password"], volumes=[bind(root / "data/mysql", "/var/lib/mysql")],
                 healthcheck=health(["CMD-SHELL",
                     'MYSQL_PWD="$$(cat /run/secrets/mysql_password)" mysql --protocol=TCP '
                     '--host=127.0.0.1 --user=c_check --database=c_check --batch --skip-column-names '
                     '--execute="SELECT 1"'], start="60s"))
    redis = service("redis", "512m", 1, ["data"], user=False)
    redis.update(read_only=False, cap_drop=[], cap_add=["CHOWN", "SETGID", "SETUID", "DAC_OVERRIDE"],
                 command=["redis-server", "--appendonly", "yes", "--maxmemory", "384mb", "--maxmemory-policy", "noeviction"],
                 volumes=[bind(root / "data/redis", "/data")], healthcheck=health(["CMD", "redis-cli", "ping"]))
    qdrant = service("qdrant", "2g", 2, ["data"])
    qdrant.update(environment={"QDRANT__TELEMETRY_DISABLED": "true", "QDRANT__STORAGE__SNAPSHOTS_PATH": "/qdrant/storage/snapshots"},
                  volumes=[bind(root / "data/qdrant", "/qdrant/storage")])
    embedding = service("embedding", "12g", 4, ["inference"])
    embedding.update(environment={"RAG_EMBEDDING_API_KEY_FILE": "/run/secrets/embedding_api_key", "MODEL_PATH": "/models/jina",
                                  "USER": "ccheck", "LOGNAME": "ccheck", "HOME": "/tmp",
                                  "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "OMP_NUM_THREADS": "4"},
                     secrets=["embedding_api_key"], volumes=[bind(root / "models/jina", "/models/jina", True)],
                     healthcheck=health(["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8002/health',timeout=5)"], start="180s"))
    qwen = service("qwen", "20g", 8, ["inference"])
    qwen.update(environment={"VLLM_API_KEY_FILE": "/run/secrets/vllm_api_key", "MODEL_PATH": "/models/qwen",
                             "GPU_MEMORY_UTILIZATION": str(memory_fraction), "HF_HUB_OFFLINE": "1",
                             "HF_HUB_DISABLE_TELEMETRY": "1", "VLLM_NO_USAGE_STATS": "1", "VLLM_CACHE_ROOT": "/cache/vllm",
                             "TRITON_CACHE_DIR": "/cache/triton", "XDG_CACHE_HOME": "/cache", "OMP_NUM_THREADS": "4",
                             "USER": "ccheck", "LOGNAME": "ccheck", "HOME": "/cache", "TORCHINDUCTOR_CACHE_DIR": "/cache/torchinductor",
                             "LIBRARY_PATH": "/opt/cuda-link"},
                secrets=["vllm_api_key"], volumes=[bind(inference.directory(site, 'qwen'), "/models/qwen", True), bind(root / "data/cache", "/cache")],
                shm_size="1g",
                healthcheck=health(["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8001/health',timeout=5)"], start="300s"))
    if inference.served_name(site) != inference.LEGACY:
        qwen['environment']['CCHECK_REVIEW_MODEL'] = inference.served_name(site)
    configure(qwen, site)
    performance = acceleration.settings(site)
    if performance:
        for target in (api, worker, init):
            target['environment'] = dict(target['environment'],
                MODEL_CHUNK_CONCURRENCY=str(performance['model_chunk_concurrency']),
                RAG_EMBEDDING_BATCH_SIZE=str(performance['embedding_batch_size']))
        qwen['entrypoint'] = ['python', '/opt/ccheck-launcher.py']
        qwen['environment'].update(MAX_NUM_SEQS=str(performance['max_num_seqs']),
            MAX_NUM_BATCHED_TOKENS=str(performance['max_num_batched_tokens']))
        qwen['volumes'].append(bind(acceleration.launcher_path(site, acceleration.qwen_launcher(site)), '/opt/ccheck-launcher.py', True))
        if acceleration.gpu_embedding(site):
            embedding['image'] = images['qwen']
            embedding['entrypoint'] = ['python', '-m', 'uvicorn', 'ccheck_embedding:app',
                '--app-dir', '/opt/ccheck-runtime', '--host', '0.0.0.0', '--port', '8002', '--workers', '1']
            embedding['volumes'].append(bind(acceleration.launcher_path(site, 'embedding_gpu.py'),
                '/opt/ccheck-runtime/ccheck_embedding.py', True))
            embedding['environment'].update(EMBEDDING_BATCH_SIZE=str(performance['embedding_batch_size']),
                EMBEDDING_TOKEN_BUDGET=str(performance['embedding_token_budget']),
                NVIDIA_DRIVER_CAPABILITIES='compute,utility', PYTORCH_CUDA_ALLOC_CONF='expandable_segments:True')
            configure(embedding, site)
    frontend = service("frontend", "256m", 1, ["edge"], user=False)
    if 'terminal_config' in site['web']:
        frontend['volumes'] = [bind(site['web']['terminal_config'], '/usr/share/nginx/html/terminal-config.json', True)]
    elif 'terminal' in site:
        from .remote_terminal import public_config_path
        frontend['volumes'] = [bind(str(public_config_path(site)), '/usr/share/nginx/html/terminal-config.json', True)]
    frontend.update(user="101:101", ports=[f"{site['web']['bind']}:{site['web']['port']}:8080"],
                    tmpfs=["/tmp:size=16777216,mode=1777", "/var/cache/nginx:size=16777216,uid=101,gid=101", "/var/run:size=1048576,uid=101,gid=101"],
                    healthcheck=health(["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/"]))
    for isolated in (worker, init, mysql, redis, qdrant, embedding, frontend):
        if isolated is embedding and acceleration.gpu_embedding(site):
            continue
        isolated['runtime'] = 'runc'
        isolated['environment'] = dict(isolated.get('environment', {}), NVIDIA_VISIBLE_DEVICES='void')
    document = {"name": project_name(site), "services": {"mysql": mysql, "redis": redis, "qdrant": qdrant,
            "embedding": embedding, "qwen": qwen, "api": api, "worker": worker, "frontend": frontend, "init": init},
            "secrets": {name: {"file": str(root / "secrets" / name)} for name in secret_names},
            "networks": specifications(site)}
    return assistant.render(site, document, images)


class Engine:
    def __init__(self, site):
        self.site = site
        self.root = Path(site["data_dir"])
        self.file = self.root / "state/compose.json"

    def compose(self, *args, timeout=300, capture=True):
        project = project_name(self.site)
        ids = run(['docker', 'ps', '-a', '-q', '--filter', 'label=com.docker.compose.project=' + project]).splitlines()
        for container in ids:
            labels = json.loads(run(['docker', 'inspect', container]))[0]['Config'].get('Labels') or {}
            if labels.get('com.docker.compose.project.config_files') != str(self.file):
                raise DeploymentError('Compose namespace belongs to another configuration; refusing cross-project action')
        return run(["docker", "compose", "--project-name", project,
                    "--file", str(self.file), *args], timeout=timeout, capture=capture)

    def management(self, command, *arguments):
        return self.compose("run", "--rm", "--no-deps", "init", "python", "-m", "app.deployment", command, *arguments, timeout=600)

    def wait(self, names, timeout=1800):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = True
            for name in names:
                container = self.compose("ps", "-a", "-q", name).strip()
                if not container:
                    ready = False
                    continue
                state = json.loads(run(["docker", "inspect", container]))[0]["State"]
                if state["Status"] in {"exited", "dead"}:
                    raise DeploymentError("container stopped during startup: " + name)
                if state.get("Health", {}).get("Status", "healthy") != "healthy" or state["Status"] != "running":
                    ready = False
            if ready:
                return
            time.sleep(5)
        raise DeploymentError("health readiness timeout: " + ", ".join(names))

    def maintenance(self, enabled):
        file = self.root / "data/control/maintenance"
        if enabled:
            file.touch(mode=0o644, exist_ok=True)
        else:
            file.unlink(missing_ok=True)

    def check_gpu_admission(self):
        container = self.compose('ps', '-q', 'qwen').strip()
        if container:
            inspection = json.loads(run(['docker', 'inspect', container]))[0]
            if inspection['State'].get('Health', {}).get('Status') == 'healthy':
                return
        processes = run(['nvidia-smi', '-i', str(self.site['gpu']), '--query-compute-apps=pid,process_name',
                         '--format=csv,noheader']).strip()
        if processes:
            raise DeploymentError('GPU has active compute workloads; prepared artifacts are retained, coordinate a free GPU slot before retrying')
        memory = run(['nvidia-smi', '-i', str(self.site['gpu']), '--query-gpu=memory.free,memory.total',
                      '--format=csv,noheader,nounits']).strip().split(',')
        fraction = acceleration.memory_fraction(self.site)
        if float(memory[0]) < float(memory[1]) * fraction:
            raise DeploymentError('GPU memory is below the selected profile budget; no processes were killed')

    def check_gpu_access(self):
        services = json.loads(self.file.read_text())['services']
        results = {}
        for name in (('qwen', 'api', 'embedding') if acceleration.gpu_embedding(self.site) else ('qwen', 'api')):
            spec = services[name]
            uid, gid = map(int, spec['user'].split(':'))
            results[name] = probe(self.site, spec['image'], cuda=name in {'qwen', 'embedding'}, uid=uid, gid=gid)
        atomic_json(self.root / 'state/gpu-access.json', results)
        return results

    def verify_gpu_access(self):
        result = {}
        for name in (('qwen', 'api', 'embedding') if acceleration.gpu_embedding(self.site) else ('qwen', 'api')):
            container = self.compose('ps', '-q', name).strip()
            if not container:
                raise DeploymentError('GPU service container is missing: ' + name)
            inspection = json.loads(run(['docker', 'inspect', container]))[0]
            validate_inspection(self.site, inspection, True)
            output = self.compose('exec', '-T', name, 'python', '-c', NVML_PROBE)
            result[name] = check_identity(self.site, output)
            if name == 'embedding':
                health = json.loads(self.compose('exec', '-T', name, 'python', '-c',
                    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8002/health').read().decode())"))
                if health.get('device') != 'cuda:0' or health.get('dtype') != 'torch.bfloat16' or health.get('dimension') != 1536:
                    raise DeploymentError('embedding service is not using the verified CUDA BF16 recipe')
                result[name]['embedding_health'] = health
        return result

    def drain(self, timeout=600):
        self.maintenance(True)
        deadline = time.monotonic() + timeout
        stable = 0
        while time.monotonic() < deadline:
            output = self.management("pending")
            pending = json.loads(output.strip().splitlines()[-1])["pending"]
            stable = stable + 1 if pending == 0 else 0
            if stable >= 2:
                return
            time.sleep(3)
        raise DeploymentError("reviews still pending; maintenance remains enabled, no model/data stopped")
