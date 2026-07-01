from app.core.security import hash_password
from app.db.models import ModelNode, ReviewFile, ReviewTask, TaskStatus, User
from app.schemas.model_response import FindingCategory, FindingSeverity, ModelReviewResponse
from app.services.model_router import ModelInvocationError


def _create_task(
    db_session_factory,
    username: str = "reviewer",
    *,
    relative_path: str = "snippet.c",
    source_text: str = "int main(void) {}",
) -> str:
    with db_session_factory() as db:
        user = User(username=username, password_hash=hash_password("reviewer-password"))
        node = ModelNode(display_name="Model", model_identifier="model", base_url="http://model")
        task = ReviewTask(
            owner=user,
            model_node=node,
            input_mode="text",
            display_name=relative_path,
            file_count=1,
            check_types=["logic"],
        )
        task.files.append(ReviewFile(relative_path=relative_path, source_text=source_text, size_bytes=len(source_text)))
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


def test_postprocess_reanchors_finding_from_comment_to_nearby_code(db_session_factory):
    from app.schemas.model_response import ModelReviewResponse
    from app.tasks.reviews import _postprocess_review_result

    source = "\n".join(
        [
            "int f(int n) {",
            "  // heap buffer overflow",
            "  char *p = malloc(n);",
            "  memcpy(p, input, 1024);",
            "  return 0;",
            "}",
        ]
    )
    task_id = _create_task(db_session_factory, source_text=source)
    result = ModelReviewResponse.model_validate(
        {
            "summary": "发现问题",
            "score": 30,
            "findings": [
                {
                    "severity": "high",
                    "category": "buffer_overflow",
                    "title": "堆缓冲区溢出",
                    "description": "memcpy 可能写超过 malloc 分配的空间。",
                    "file_path": "snippet.c",
                    "line": 2,
                }
            ],
        }
    )

    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        processed = _postprocess_review_result(task, result)

    assert len(processed.findings) == 1
    assert processed.findings[0].line == 3


def test_postprocess_downgrades_null_pointer_findings_to_suggestions(db_session_factory):
    from app.tasks.reviews import _postprocess_review_result

    source = "\n".join(
        [
            "void f(void) {",
            "  CAN->TSR = CAN_TSR_RQCP0;",
            "}",
        ]
    )
    task_id = _create_task(db_session_factory, source_text=source)
    result = ModelReviewResponse.model_validate(
        {
            "summary": "发现问题",
            "score": 30,
            "findings": [
                {
                    "severity": "high",
                    "category": "pointer_safety",
                    "title": "CAN 指针可能为空",
                    "description": "CAN 指针未初始化可能导致空指针访问。",
                    "file_path": "snippet.c",
                    "line": 2,
                }
            ],
        }
    )

    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        processed = _postprocess_review_result(task, result)

    finding = processed.findings[0]
    assert finding.severity == FindingSeverity.SUGGESTION
    assert finding.category == FindingCategory.MAINTAINABILITY
    assert "固定映射地址" in finding.description


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


def test_run_review_task_persists_strict_finding_fields(db_session_factory, monkeypatch):
    from app.core.config import get_settings
    import app.tasks.reviews as review_tasks
    from app.tasks.reviews import run_review_task

    monkeypatch.setattr(review_tasks, "SessionLocal", db_session_factory)
    get_settings.cache_clear()

    async def fake_invoke(_db, _task_id, retry_instruction=None):
        return ModelReviewResponse(
            summary="发现问题。",
            score=80,
            findings=[
                {
                    "severity": "high",
                    "category": "memory_safety",
                    "title": "固定大小缓冲区写入",
                    "description": "目标缓冲区容量固定，写入前未验证长度。",
                    "file_path": "src/lcd.c",
                    "line": 3,
                }
            ],
        )

    monkeypatch.setattr("app.tasks.reviews.invoke_selected_model", fake_invoke)
    task_id = _create_task(
        db_session_factory,
        relative_path="project/src/lcd.c",
        source_text="\n".join(
            [
                "void draw(void) {",
                "    char name[8];",
                "    strcpy(name, input);",
                "}",
            ]
        ),
    )

    run_review_task(task_id)

    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        finding = task.report.result_json["findings"][0]
        assert finding["line"] == 3
        assert set(finding) == {"severity", "category", "title", "description", "file_path", "line", "code_snippet"}
        assert [item["line"] for item in finding["code_snippet"]] == [1, 2, 3, 4]
        assert finding["code_snippet"][2]["kind"] == "removed"

    get_settings.cache_clear()


def test_run_review_task_filters_findings_anchored_to_static_data_rows(db_session_factory, monkeypatch):
    from app.core.config import get_settings
    import app.tasks.reviews as review_tasks
    from app.tasks.reviews import run_review_task

    monkeypatch.setattr(review_tasks, "SessionLocal", db_session_factory)
    get_settings.cache_clear()

    async def fake_invoke(_db, _task_id, retry_instruction=None):
        return ModelReviewResponse(
            summary="发现问题。",
            score=20,
            findings=[
                {
                    "severity": "high",
                    "category": "memory_safety",
                    "title": "固定大小缓冲区写入",
                    "description": "模型误把静态点阵数据识别为写入。",
                    "file_path": "src/lcd.c",
                    "line": 3,
                },
                {
                    "severity": "high",
                    "category": "buffer_overflow",
                    "title": "未限制字符串复制",
                    "description": "复制外部输入前未验证目标缓冲区容量。",
                    "file_path": "src/lcd.c",
                    "line": 6,
                },
            ],
        )

    monkeypatch.setattr("app.tasks.reviews.invoke_selected_model", fake_invoke)
    task_id = _create_task(
        db_session_factory,
        relative_path="src/lcd.c",
        source_text="\n".join(
            [
                "static const unsigned short font[] = {",
                "    /* '5' */",
                "    0xCE60, 0xCC30, 0x0C18, 0x0C0C,",
                "};",
                "void copy_name(const char *input) {",
                "    strcpy(name, input);",
                "}",
            ]
        ),
    )

    run_review_task(task_id)

    with db_session_factory() as db:
        task = db.get(ReviewTask, task_id)
        findings = task.report.result_json["findings"]
        assert len(findings) == 1
        assert findings[0]["title"] == "未限制字符串复制"
        assert findings[0]["line"] == 6

    get_settings.cache_clear()
