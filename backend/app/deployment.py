"""Non-HTTP management entry points used by the release installer."""
import argparse
import importlib
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, or_, select, text

from app.core.config import get_settings
from app.db.models import CodeProject, ModelNode, ReviewTask, TaskStatus
from app.db.session import SessionLocal, engine


def migrate():
    if engine.dialect.name == "mysql":
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE IF NOT EXISTS alembic_version "
                                    "(version_num VARCHAR(64) NOT NULL PRIMARY KEY)"))
            columns = inspect(connection).get_columns("alembic_version")
            if columns[0]["type"].length < 64:
                connection.execute(text("ALTER TABLE alembic_version MODIFY version_num VARCHAR(64) NOT NULL"))
    command.upgrade(Config("alembic.ini"), "head")


def review_model():
    name = os.environ.get('CCHECK_REVIEW_MODEL', 'qwen2.5-coder-32b-instruct-fp8')
    labels = {'qwen2.5-coder-32b-instruct-fp8': 'Qwen2.5-Coder-32B FP8',
              'qwen3-coder-30b-a3b-instruct-fp8': 'Qwen3-Coder-30B-A3B FP8'}
    if name not in labels:
        raise RuntimeError('unsupported deployed review model')
    return name, labels[name]


def register_model():
    import httpx

    settings = get_settings()
    name, label = review_model()
    base = "http://qwen:8001"
    response = httpx.get(base + "/v1/models", headers={"Authorization": "Bearer " + settings.vllm_api_key},
                         timeout=30, trust_env=False)
    response.raise_for_status()
    if name not in {row["id"] for row in response.json()["data"]}:
        raise RuntimeError("served model does not match release")
    with SessionLocal() as db:
        nodes = list(db.scalars(select(ModelNode).where(ModelNode.model_identifier == name)).all())
        if len(nodes) > 1:
            raise RuntimeError("duplicate model nodes require administrator resolution")
        node = nodes[0] if nodes else ModelNode(display_name=label, model_identifier=name)
        for previous in db.scalars(select(ModelNode).where(ModelNode.model_identifier != name)).all():
            previous.is_default = False
            if previous.base_url == base:
                previous.is_enabled = False
        node.base_url = base
        node.api_key = settings.vllm_api_key
        node.timeout_seconds = 600
        node.is_enabled = True
        node.is_default = True
        node.gpu_indices = [0]
        node.tensor_parallel_size = 1
        if not nodes:
            db.add(node)
        db.commit()
        print(json.dumps({"model_id": node.id}))


def pending():
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(ReviewTask).where(
            or_(ReviewTask.status.in_([TaskStatus.RUNNING, TaskStatus.QUEUED]),
                ReviewTask.graph_build_status.in_(["queued", "running"]))))
        print(json.dumps({"pending": count}))


def disable_model():
    with SessionLocal() as db:
        nodes = db.scalars(select(ModelNode).where(ModelNode.base_url == "http://qwen:8001")).all()
        for node in nodes:
            node.is_enabled = False
            node.is_default = False
        db.commit()
        print(json.dumps({"disabled": len(nodes)}))


def audit_core():
    policy = json.loads(Path("/opt/release/core-policy.json").read_text())
    artifacts = []
    for name in policy["compiled_modules"]:
        module = importlib.import_module(name)
        compiled = getattr(module, '__compiled__', None)
        folder = Path('/app/backend') / Path(name.replace('.', '/')).parent
        matches = list(folder.glob(name.rsplit('.', 1)[1] + '.*.so'))
        # Nuitka preserves a virtual .py __file__ for compatibility and uses its own loader.
        if compiled is None or not compiled.module or compiled.main != name or len(matches) != 1:
            raise RuntimeError('core module is not the expected compiled extension: ' + name)
        with matches[0].open('rb') as stream:
            header = stream.read(20)
        if header[:6] != b'\x7fELF\x02\x01' or header[16:20] != b'\x03\x00\x3e\x00':
            raise RuntimeError('core artifact is not an ELF64 x86-64 shared object: ' + name)
        if Path(compiled.containing_dir).resolve() != folder.resolve():
            raise RuntimeError('compiled module loaded outside the protected package: ' + name)
        if (folder / (name.rsplit('.', 1)[1] + '.py')).exists():
            raise RuntimeError('plaintext core source remains: ' + name)
        functions = [value for value in vars(module).values() if type(value).__name__ == 'compiled_function'
                     and getattr(value, '__module__', None) == name]
        if not functions:
            raise RuntimeError('native module contains no compiled first-party functions: ' + name)
        artifacts.append({'module': name, 'artifact': matches[0].name, 'compiled_functions': len(functions)})
    from app.services.code_index.clangd import parse_with_libclang
    parsed = parse_with_libclang("probe.c", "int release_probe(int x) { return x + 1; }")
    if parsed is None or not any(row.name == "release_probe" for row in parsed.symbols):
        raise RuntimeError("real libclang parse failed")
    print(json.dumps({"native_core_modules": len(policy["compiled_modules"]), "libclang": True, 'artifacts': artifacts}))


def verify_runtime():
    import math
    import httpx

    settings = get_settings()
    with httpx.Client(timeout=180, trust_env=False) as client:
        response = client.post("http://qwen:8001/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.vllm_api_key},
            json={"model": review_model()[0], "temperature": 0, "max_tokens": 64,
                  "messages": [{"role": "user", "content": 'Return {"ok":true}.'}],
                  "response_format": {"type": "json_schema", "json_schema": {"name": "probe", "strict": True,
                      "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                 "required": ["ok"], "additionalProperties": False}}}})
        response.raise_for_status()
        assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"ok": True}
        response = client.post("http://embedding:8002/v1/embeddings",
            headers={"Authorization": "Bearer " + settings.rag_embedding_api_key},
            json={"model": settings.rag_embedding_model, "input": [
                "Find the most relevant code snippet given the following query:\nreturn the sum of two integers",
                "Candidate code snippet:\nint add(int a,int b) { return a+b; }",
                'Candidate code snippet:\nFILE *f=fopen("data.txt","r"); fread(buf,1,16,f); fclose(f);']})
        response.raise_for_status()
        vectors = [row["embedding"] for row in response.json()["data"]]
        assert all(len(v) == 1536 and all(math.isfinite(x) for x in v) for v in vectors)
        assert all(abs(sum(x*x for x in v) - 1) < 0.01 for v in vectors)
        scores = [sum(x*y for x, y in zip(vectors[0], v)) for v in vectors[1:]]
        assert scores[0] > scores[1], "embedding semantic ranking failed"
        unauthorized = client.post("http://embedding:8002/v1/embeddings",
            json={"model": settings.rag_embedding_model, "input": "test"})
        assert unauthorized.status_code == 401
        print(json.dumps({"structured_generation": True, "embedding_dimension": 1536, "similarities": scores}))


def data_health():
    import httpx
    import redis
    settings = get_settings()
    with engine.connect() as connection:
        assert connection.scalar(text('SELECT 1')) == 1
    assert redis.Redis.from_url(settings.redis_url, socket_timeout=5).ping()
    response = httpx.get(settings.rag_qdrant_url + '/', timeout=15, trust_env=False)
    response.raise_for_status()
    assert response.json()['version'] == '1.15.5'
    print(json.dumps({'mysql': True, 'redis': True, 'qdrant': True}))


def audit_index(task_id):
    import httpx
    from app.services.code_index.qdrant import QdrantCodeIndexClient
    settings = get_settings()
    with SessionLocal() as db:
        project = db.scalar(select(CodeProject).where(CodeProject.task_id == task_id))
        assert project is not None
        stats = project.stats_json
        assert not stats['parser_degraded'] and stats['embedding_dimension'] == 1536
        assert stats['embedding_backend'] == 'openai-compatible' and stats['qdrant_points'] > 0
        with httpx.Client(timeout=30, trust_env=False) as client:
            base = settings.rag_qdrant_url + '/collections/' + settings.rag_qdrant_collection
            condition = {'must': [{'key': 'project_id', 'match': {'value': project.id}}]}
            result = client.post(base + '/points/scroll', json={'filter': condition, 'limit': 100,
                                 'with_payload': True, 'with_vector': True})
            result.raise_for_status()
            points = result.json()['result']['points']
            assert len(points) == stats['qdrant_points']
            assert all(point['payload']['project_id'] == project.id for point in points)
            vector = points[0]['vector']
            adapter = QdrantCodeIndexClient(settings)
            own = adapter.search_sync(vector, project_id=project.id, limit=20)
            foreign = adapter.search_sync(vector, project_id='missing-project-' + project.id, limit=20)
            assert own and all(point['payload']['project_id'] == project.id for point in own)
            assert foreign == []
        print(json.dumps({'project_id': project.id, 'stats': stats, 'project_vector_filter': True}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "register-model", "disable-model", "pending", "audit-core", "verify-runtime", "data-health", "audit-index"])
    parser.add_argument('--task-id')
    args = parser.parse_args()
    if args.command == 'audit-index' and not args.task_id:
        parser.error('--task-id is required for audit-index')
    {"migrate": migrate, "register-model": register_model, "pending": pending, "audit-core": audit_core,
     "verify-runtime": verify_runtime, "disable-model": disable_model, 'data-health': data_health,
     'audit-index': lambda: audit_index(args.task_id)}[args.command]()


if __name__ == "__main__":
    main()
