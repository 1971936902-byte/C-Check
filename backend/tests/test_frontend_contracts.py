from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.db.models import CodeFile, CodeProject, ModelNode, ReviewTask, User


def test_graph_files_keep_real_ids_and_enforce_task_ownership(db_session):
    from app.api.code_index import list_files

    owner = User(username="owner", password_hash="unused")
    other = User(username="other", password_hash="unused")
    model = ModelNode(display_name="Archived model", model_identifier="old", base_url="http://unused", is_enabled=False)
    task = ReviewTask(owner=owner, model_node=model, display_name="test", input_mode="folder")
    project = CodeProject(task=task, source_hash="source", parser_version="test")
    file = CodeFile(project=project, relative_path="src/main.c", content_hash="hash", size_bytes=10)
    db_session.add_all([file, other])
    db_session.commit()

    assert [item.model_dump() for item in list_files(task.id, owner, db_session)] == [{"id": file.id, "file_path": "src/main.c"}]
    with pytest.raises(HTTPException) as error:
        list_files(task.id, other, db_session)
    assert error.value.status_code == 403


def test_history_returns_disabled_model_name_and_files_endpoint(db_session):
    from app.api.deps import get_current_user
    from app.main import app

    owner = User(username="history-owner", password_hash="unused")
    model = ModelNode(display_name="Disabled review model", model_identifier="old", base_url="http://unused", is_enabled=False)
    task = ReviewTask(owner=owner, model_node=model, display_name="history", input_mode="file")
    project = CodeProject(task=task, source_hash="source", parser_version="test")
    file = CodeFile(project=project, relative_path="main.c", content_hash="hash", size_bytes=10)
    db_session.add(file)
    db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(app) as client:
            response = client.get("/api/reviews")
            assert response.status_code == 200
            assert response.json()["items"][0]["model_display_name"] == "Disabled review model"
            response = client.get(f"/api/code-index/{task.id}/files")
            assert response.status_code == 200
            assert response.json() == [{"id": file.id, "file_path": "main.c"}]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
