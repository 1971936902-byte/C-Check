from app.core.security import hash_password
from app.db.models import ModelNode, ReviewFile, ReviewTask, TaskStatus, User
from app.schemas.model_response import ModelReviewResponse
from app.services.model_router import ModelInvocationError


def _create_task(db_session_factory, username: str = "reviewer") -> str:
    with db_session_factory() as db:
        user = User(username=username, password_hash=hash_password("reviewer-password"))
        node = ModelNode(display_name="Model", model_identifier="model", base_url="http://model")
        task = ReviewTask(
            owner=user,
            model_node=node,
            input_mode="text",
            display_name="snippet.c",
            file_count=1,
            check_types=["logic"],
        )
        task.files.append(ReviewFile(relative_path="snippet.c", source_text="int main(void) {}", size_bytes=17))
        db.add_all([user, node, task])
        db.commit()
        return task.id


def test_run_review_task_retries_and_persists_model_log(db_session_factory, monkeypatch):
    from app.core.config import get_settings
    import app.tasks.reviews as review_tasks
    from app.tasks.reviews import run_review_task

    monkeypatch.setenv("MODEL_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(review_tasks, "SessionLocal", db_session_factory)
    get_settings.cache_clear()
    calls = {"count": 0, "retry_instruction": None}

    async def fake_invoke(_db, _task_id, retry_instruction=None):
        calls["count"] += 1
        calls["retry_instruction"] = retry_instruction
        if calls["count"] == 1:
            raise ModelInvocationError(
                "model returned an invalid structured response",
                raw_response="not valid json",
                details="expected JSON object",
            )
        assert retry_instruction and "not valid json" in retry_instruction
        return ModelReviewResponse(summary="重试后成功。", score=100, findings=[])

    monkeypatch.setattr("app.tasks.reviews.invoke_selected_model", fake_invoke)
    task_id = _create_task(db_session_factory)

    run_review_task(task_id)

    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.error_message is None
        assert task.report is not None
        assert calls["count"] == 2
        assert "Attempt 1 failed" in task.model_log
        assert "Raw model response" in task.model_log
        assert "not valid json" in task.model_log
        assert "Attempt 2 succeeded" in task.model_log

    get_settings.cache_clear()


def test_run_review_task_reports_audit_failure_after_max_attempts(db_session_factory, monkeypatch):
    from app.core.config import get_settings
    import app.tasks.reviews as review_tasks
    from app.tasks.reviews import run_review_task

    monkeypatch.setenv("MODEL_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(review_tasks, "SessionLocal", db_session_factory)
    get_settings.cache_clear()
    calls = {"count": 0, "retry_instructions": []}

    async def fake_invoke(_db, _task_id, retry_instruction=None):
        calls["count"] += 1
        calls["retry_instructions"].append(retry_instruction)
        raise ModelInvocationError(
            "model returned an invalid structured response",
            raw_response='{"summary":"missing required fields"}',
            details="findings: Field required",
        )

    monkeypatch.setattr("app.tasks.reviews.invoke_selected_model", fake_invoke)
    task_id = _create_task(db_session_factory)

    run_review_task(task_id)

    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        assert task.status == TaskStatus.FAILED
        assert task.report is None
        assert calls["count"] == 3
        assert calls["retry_instructions"][0] is None
        assert "backend JSON schema audit failed" in calls["retry_instructions"][1]
        assert "findings: Field required" in calls["retry_instructions"][1]
        assert task.error_message == "model output audit failed after 3 attempts"
        assert "Final audit result: failed after 3 attempt(s)." in task.model_log

    get_settings.cache_clear()
