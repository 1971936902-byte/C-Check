from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models import CodeProject, ModelNode, ReviewContext, ReviewFile, ReviewTask, TaskStatus, User
from app.schemas.model_response import ModelReviewResponse


def make_task(db):
    task = ReviewTask(
        owner=User(username="graph-owner", password_hash="unused"),
        model_node=ModelNode(display_name="model", model_identifier="test", base_url="http://unused"),
        input_mode="file", display_name="main.c", status=TaskStatus.COMPLETED,
        progress=100, duration_ms=123, finding_count=0,
    )
    task.files = [ReviewFile(relative_path="main.c", source_text="int main(void) { return 0; }", size_bytes=28)]
    db.add(task)
    db.commit()
    return task


def test_deployment_drain_waits_for_manual_graph(db_session_factory, monkeypatch, capsys):
    import json
    from app import deployment
    monkeypatch.setattr(deployment, "SessionLocal", db_session_factory)
    with db_session_factory() as db:
        task = make_task(db)
        task.graph_build_status = "running"
        db.commit()
    deployment.pending()
    assert json.loads(capsys.readouterr().out)["pending"] == 1
    with db_session_factory() as db:
        task = db.get(ReviewTask, task.id)
        task.graph_build_status = "completed"
        db.commit()
    deployment.pending()
    assert json.loads(capsys.readouterr().out)["pending"] == 0


def test_review_finishes_without_supplemental_graph(db_session_factory, monkeypatch):
    import app.tasks.reviews as worker
    with db_session_factory() as db:
        task_id = make_task(db).id
    async def invoke(*args, **kwargs):
        return ModelReviewResponse(summary="ok", score=100, findings=[])
    build = Mock(side_effect=AssertionError("review finalization must not build a graph"))
    monkeypatch.setattr(worker, "SessionLocal", db_session_factory)
    monkeypatch.setattr(worker, "invoke_selected_model", invoke)
    monkeypatch.setattr("app.services.code_index.context_builder.build_rag_context", build)
    monkeypatch.setattr("app.services.code_index.indexer.build_code_index", build)
    worker.run_review_task(task_id)
    build.assert_not_called()
    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        assert task.status == TaskStatus.COMPLETED and task.progress == 100
        assert task.report is not None and task.code_project is None
        assert task.graph_build_status == "idle"


def test_graph_reads_never_generate_and_cached_observability_is_read_only(db_session, monkeypatch):
    from app.api.code_index import build_status, observability
    task = make_task(db_session)
    forbidden = Mock(side_effect=AssertionError("read must not perform indexing or retrieval"))
    monkeypatch.setattr("app.services.code_index.indexer.build_code_index", forbidden)
    monkeypatch.setattr("app.services.code_index.retriever.retrieve_context_diagnostics", forbidden)
    assert build_status(task.id, task.owner, db_session).status == "idle"
    assert observability(task.id, task.owner, db_session).project_id is None
    project = CodeProject(task=task, source_hash="x", parser_version="test", stats_json={
        "files": 1, "observability": {"selected_count": 2},
    })
    db_session.add(project)
    db_session.commit()
    result = observability(task.id, task.owner, db_session)
    assert result.selected_count == 2 and result.stats == {"files": 1}
    forbidden.assert_not_called()


def test_only_explicit_build_enqueues_and_duplicates_are_ignored(db_session, monkeypatch):
    from app.api.deps import get_current_user
    from app.main import app
    task = make_task(db_session)
    dispatch = Mock()
    monkeypatch.setattr("app.tasks.code_index.dispatch_graph_build.delay", dispatch)
    app.dependency_overrides[get_current_user] = lambda: task.owner
    try:
        with TestClient(app) as client:
            assert client.get(f"/api/code-index/{task.id}/build").json()["status"] == "idle"
            assert client.get(f"/api/code-index/{task.id}/observability").status_code == 200
            dispatch.assert_not_called()
            for _ in range(2):
                result = client.post(f"/api/code-index/{task.id}/build")
                assert result.status_code == 202 and result.json()["status"] == "queued"
            dispatch.assert_called_once()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_build_permissions_and_running_review_rejected(db_session, monkeypatch):
    from app.api.code_index import build_index, build_status
    task = make_task(db_session)
    other = User(username="other", password_hash="unused")
    db_session.add(other)
    db_session.commit()
    dispatch = Mock()
    monkeypatch.setattr("app.tasks.code_index.dispatch_graph_build.delay", dispatch)
    for action in (build_status, build_index):
        with pytest.raises(HTTPException) as error:
            action(task.id, other, db_session)
        assert error.value.status_code == 403
    task.status = TaskStatus.RUNNING
    db_session.commit()
    with pytest.raises(HTTPException) as error:
        build_index(task.id, task.owner, db_session)
    assert error.value.status_code == 409
    dispatch.assert_not_called()


def test_dispatch_failure_is_retryable(db_session, monkeypatch):
    from app.api.code_index import build_index
    task = make_task(db_session)
    dispatch = Mock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr("app.tasks.code_index.dispatch_graph_build.delay", dispatch)
    with pytest.raises(HTTPException) as error:
        build_index(task.id, task.owner, db_session)
    assert error.value.status_code == 503
    db_session.refresh(task)
    assert task.graph_build_status == "failed"
    dispatch.side_effect = None
    assert build_index(task.id, task.owner, db_session).status == "queued"


def test_manual_worker_builds_once_preserves_review_and_evidence(db_session_factory, monkeypatch):
    import app.tasks.code_index as worker
    monkeypatch.setattr(worker, "SessionLocal", db_session_factory)
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(_env_file=None, allow_insecure_defaults=True))
    with db_session_factory() as db:
        task = make_task(db)
        task.graph_build_id, task.graph_build_status = "build-1", "queued"
        evidence = ReviewContext(task=task, context_text="original review evidence", metadata_json={})
        db.add(evidence)
        db.commit()
        task_id, evidence_id = task.id, evidence.id
    worker.run_graph_build(task_id, "stale-id")
    worker.run_graph_build(task_id, "build-1")
    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        assert task.graph_build_status == "completed"
        assert task.code_project.stats_json["files"] == 1
        assert task.code_project.stats_json["symbols"] >= 1
        assert "observability" in task.code_project.stats_json
        assert task.status == TaskStatus.COMPLETED and task.progress == 100
        assert task.duration_ms == 123 and task.finding_count == 0
        assert task.review_contexts[0].id == evidence_id
    forbidden = Mock(side_effect=AssertionError("duplicate completed delivery"))
    monkeypatch.setattr(worker, "build_code_index", forbidden)
    worker.run_graph_build(task_id, "build-1")
    forbidden.assert_not_called()


def test_worker_failure_does_not_fail_review_and_redelivery_can_resume(db_session_factory, monkeypatch):
    import app.tasks.code_index as worker
    monkeypatch.setattr(worker, "SessionLocal", db_session_factory)
    monkeypatch.setattr(worker, "build_code_index", Mock(side_effect=RuntimeError("embedding failure")))
    with db_session_factory() as db:
        task = make_task(db)
        task.graph_build_id, task.graph_build_status = "interrupted", "running"
        db.commit()
        task_id = task.id
    worker.run_graph_build(task_id, "interrupted")
    worker.build_code_index.assert_called_once()
    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        assert task.graph_build_status == "failed" and task.graph_build_error
        assert task.status == TaskStatus.COMPLETED and task.duration_ms == 123
        assert task.code_project is None


def test_graph_migration_preserves_existing_rows():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    path = Path(__file__).parents[1] / "alembic/versions/0016_manual_graph_build.py"
    spec = importlib.util.spec_from_file_location("manual_graph_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE review_tasks (id VARCHAR(36) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO review_tasks (id) VALUES ('existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                row = connection.execute(text("SELECT * FROM review_tasks")).mappings().one()
                assert row["id"] == "existing" and row["graph_build_status"] == "idle"
                assert row["graph_build_error"] is None and row["graph_build_id"] is None
                migration.downgrade()
            assert connection.execute(text("SELECT id FROM review_tasks")).scalar_one() == "existing"
    finally:
        engine.dispose()
