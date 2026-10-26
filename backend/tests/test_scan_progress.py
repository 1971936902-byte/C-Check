import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.db.models import ModelNode, ReviewFile, ReviewTask, User
from app.schemas.model_response import ModelReviewResponse
from app.services import model_router as router


def source(name):
    return ReviewFile(relative_path=name, source_text="int value;", size_bytes=10)


def node():
    return ModelNode(id="test", display_name="test", model_identifier="test", base_url="http://unused")


def settings(**kwargs):
    return Settings(_env_file=None, allow_insecure_defaults=True, **kwargs)


def result():
    return ModelReviewResponse(summary="ok", score=100, findings=[])


def test_file_completion_waits_for_all_batches_out_of_order(monkeypatch):
    a, b, c = source("a.c"), source("b.c"), source("c.c")
    batches = [[a], [a, b], [c]]
    monkeypatch.setattr(router, "_chunk_review_batches", lambda *args, **kwargs: batches)
    recorded = []

    async def run():
        third, first = asyncio.Event(), asyncio.Event()

        async def invoke(*, prompt, **kwargs):
            if prompt == "1":
                await first.wait()
            if prompt == "3":
                await third.wait()
            return result()

        def progress(done, total):
            recorded.append((done, total))
            if done == 1:
                third.set()
            elif done == 2:
                first.set()

        monkeypatch.setattr(router, "invoke_model", invoke)
        return await asyncio.wait_for(router._invoke_chunked_review(
            node=node(), files=[a, b, c], prompt="review", settings=settings(model_chunk_concurrency=3),
            batch_prompt_builder=lambda prompt, index, count, files: str(index),
            file_progress_callback=progress,
        ), timeout=3)

    asyncio.run(run())
    assert recorded == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_rechunk_resets_file_counts(monkeypatch):
    a, b = source("a.c"), source("b.c")
    monkeypatch.setattr(router, "_chunk_review_batches", lambda *args, **kwargs: [[a], [b]])
    recorded = []

    async def invoke(*, files, settings, **kwargs):
        if files[0].relative_path == "b.c" and settings.model_chunk_max_chars == 12000:
            raise router.ModelInvocationError("context window is too small")
        return result()

    monkeypatch.setattr(router, "invoke_model", invoke)
    asyncio.run(router._invoke_chunked_review(
        node=node(), files=[a, b], prompt="review",
        settings=settings(model_chunk_concurrency=1, model_chunk_max_chars=12000),
        batch_prompt_builder=lambda *args: "review", file_progress_callback=lambda d, t: recorded.append((d, t)),
    ))
    assert recorded == [(0, 2), (1, 2), (0, 2), (1, 2), (2, 2)]


def test_failed_batch_does_not_finish_file(monkeypatch):
    a = source("a.c")
    monkeypatch.setattr(router, "_chunk_review_batches", lambda *args, **kwargs: [[a], [a]])
    recorded = []

    async def invoke(*, prompt, **kwargs):
        if prompt == "2":
            raise router.ModelInvocationError("failed")
        return result()

    monkeypatch.setattr(router, "invoke_model", invoke)
    with pytest.raises(router.ModelInvocationError):
        asyncio.run(router._invoke_chunked_review(
            node=node(), files=[a], prompt="review", settings=settings(model_chunk_concurrency=1),
            batch_prompt_builder=lambda p, i, n, f: str(i),
            file_progress_callback=lambda d, t: recorded.append((d, t)),
        ))
    assert recorded == [(0, 1), (0, 1)]


def test_candidate_completion_is_before_postprocessing(monkeypatch):
    recorded = []
    monkeypatch.setattr(router, "_rag_context", lambda *args, **kwargs: "")

    async def invoke(**kwargs):
        assert recorded == [(0, 2)]
        return result()

    async def finalize(**kwargs):
        assert recorded == [(0, 2), (2, 2)]
        return result()

    monkeypatch.setattr(router, "invoke_model", invoke)
    monkeypatch.setattr(router, "_finalize_candidate_review", finalize)
    asyncio.run(router._invoke_candidate_review(
        db=None, task=SimpleNamespace(model_log=None), node=node(), files=[source("a.c"), source("b.h")],
        prompt="review", settings=settings(), retry_instruction=None,
        file_progress_callback=lambda d, t: recorded.append((d, t)),
    ))


def test_unknown_history_counts_remain_unknown():
    task = ReviewTask(file_count=5, progress=100)
    assert task.scan_file_remaining is None
    task.scan_file_total = 3
    task.scan_file_completed = 2
    assert task.scan_file_remaining == 1


def test_selected_model_persists_counts_with_chunk_percentage(monkeypatch, db_session_factory):
    with db_session_factory() as db:
        user = User(username="scan-counter", password_hash="unused")
        model = node()
        task = ReviewTask(owner=user, model_node=model, input_mode="archive", display_name="scan",
                          file_count=2, files=[source("a.c"), source("b.c")], check_types=["logic"])
        db.add(task)
        db.commit()
        task_id = task.id
        monkeypatch.setattr("app.services.prompts.get_active_prompt", lambda db: SimpleNamespace(body="review"))
        monkeypatch.setattr(router, "get_settings", lambda: settings(rag_enabled=False, rag_candidate_scan_enabled=False))
        monkeypatch.setattr(router, "_should_chunk", lambda *args, **kwargs: True)
        monkeypatch.setattr(router, "_rag_review_unit_files", lambda *args: [])
        monkeypatch.setattr(router, "_chunk_review_batches", lambda *args, **kwargs: [[task.files[0]], task.files])
        monkeypatch.setattr(router, "_batch_prompt", lambda *args: "review")

        async def invoke(**kwargs):
            return result()

        monkeypatch.setattr(router, "invoke_model", invoke)
        asyncio.run(router.invoke_selected_model(db, task_id))
    with db_session_factory() as db:
        saved = db.get(ReviewTask, task_id)
        assert (saved.scan_file_completed, saved.scan_file_total, saved.scan_file_remaining) == (2, 2, 0)
        assert saved.progress == 95


def test_scan_counter_migration_preserves_old_rows(tmp_path, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = Path(__file__).resolve().parents[1] / "alembic/versions/0017_review_scan_progress.py"
    spec = importlib.util.spec_from_file_location("scan_counter_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE review_tasks (id TEXT PRIMARY KEY, file_count INTEGER)"))
            connection.execute(text("INSERT INTO review_tasks VALUES ('old-task', 7)"))
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            row = connection.execute(text("SELECT * FROM review_tasks")).mappings().one()
            assert row == {"id": "old-task", "file_count": 7, "scan_file_total": None, "scan_file_completed": None}
            migration.downgrade()
            assert {c["name"] for c in inspect(connection).get_columns("review_tasks")} == {"id", "file_count"}
            assert connection.execute(text("SELECT file_count FROM review_tasks")).scalar() == 7
    finally:
        engine.dispose()


def test_gpu_endpoint_is_admin_only_without_model_calls(monkeypatch, db_session_factory):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.security import create_access_token
    from app.core.config import get_settings
    from app.services import resources

    secret = "test-gpu-jwt-secret-with-at-least-32-characters"
    monkeypatch.setenv("JWT_SECRET", secret)
    get_settings.cache_clear()
    with db_session_factory() as db:
        admin = User(username="gpu-admin", password_hash="unused", role="admin")
        regular = User(username="gpu-regular", password_hash="unused")
        db.add_all([admin, regular])
        db.commit()
        admin_id, regular_id = admin.id, regular.id

    monkeypatch.setattr(resources, "_gpu_devices", lambda: [
        {"index": 0, "name": "Test GPU", "memory_used_mb": 1000, "memory_total_mb": 2000}
    ])

    def fail(*args):
        raise AssertionError("GPU-only API must not poll model endpoints")

    monkeypatch.setattr(resources, "_model_metrics", fail)
    with TestClient(app) as client:
        assert client.get("/api/admin/resources/gpus").status_code == 401
        for user_id, status in ((regular_id, 403), (admin_id, 200)):
            token = create_access_token(user_id, 0, secret, timedelta(minutes=5))
            response = client.get("/api/admin/resources/gpus", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == status
            if status == 200:
                assert response.json()["gpus"][0]["memory_total_mb"] == 2000
