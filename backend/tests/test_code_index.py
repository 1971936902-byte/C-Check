import asyncio

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import CodeChunk, CodeEdge, CodeProject, CodeSymbol, ModelNode, ReviewContext, ReviewFile, ReviewTask, User
from app.schemas.model_response import FindingCategory, FindingSeverity, ReviewFinding
from app.schemas.model_response import ModelReviewResponse
from app.services.code_index.context_builder import build_rag_context
from app.services.code_index.indexer import build_code_index
from app.services.code_index.parser import parse_c_source
from app.services.model_router import invoke_selected_model


def _make_task() -> ReviewTask:
    user = User(username="rag-user", password_hash=hash_password("rag-password"))
    node = ModelNode(
        display_name="RAG node",
        model_identifier="review-model",
        base_url="http://model-node",
        is_enabled=True,
    )
    task = ReviewTask(
        owner=user,
        model_node=node,
        input_mode="folder",
        display_name="driver",
        file_count=2,
        check_types=["memory_safety"],
    )
    task.files.extend(
        [
            ReviewFile(
                relative_path="src/driver.c",
                source_text=(
                    '#include "helpers.h"\n'
                    "int driver_entry(int value) {\n"
                    "    return helper_copy(value);\n"
                    "}\n"
                ),
                size_bytes=88,
            ),
            ReviewFile(
                relative_path="src/helpers.c",
                source_text=(
                    "int helper_copy(int value) {\n"
                    "    char buf[4];\n"
                    "    buf[value] = 0;\n"
                    "    return value;\n"
                    "}\n"
                ),
                size_bytes=96,
            ),
        ]
    )
    return task


def test_parse_c_source_extracts_symbols_includes_and_calls():
    parsed = parse_c_source(
        "src/main.c",
        '#include "helpers.h"\n#define MAX_LEN 32\nint main(void) {\n    return helper_copy(MAX_LEN);\n}\n',
    )

    assert parsed.includes[0].target == "helpers.h"
    assert {symbol.name for symbol in parsed.symbols} >= {"MAX_LEN", "main"}
    assert parsed.calls[0].caller_name == "main"
    assert parsed.calls[0].callee_name == "helper_copy"


def test_build_code_index_persists_graph_entities(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    db_session.commit()

    assert db_session.query(CodeProject).count() == 1
    assert project.stats_json["files"] == 2
    assert db_session.query(CodeSymbol).filter_by(name="helper_copy", kind="function").one()
    assert db_session.query(CodeChunk).filter_by(chunk_kind="file_summary").count() == 2
    assert db_session.query(CodeChunk).filter_by(chunk_kind="callsite").count() >= 1
    call_edge = db_session.query(CodeEdge).filter_by(edge_type="FUNCTION_CALLS_FUNCTION").one()
    assert call_edge.metadata_json["callee_name"] == "helper_copy"
    assert call_edge.target_id is not None


def test_rag_context_includes_cross_file_callee_definition(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    context = build_rag_context(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_context_max_chars=4000),
    )

    assert "RAG" in context
    assert "Evidence E1" in context
    assert "src/helpers.c" in context
    assert "helper_copy" in context
    assert "buf[value]" in context
    assert db_session.query(ReviewContext).count() == 1
    assert task.review_contexts[0].evidence_items[0].evidence_key == "E1"


def test_invoke_selected_model_adds_rag_context_to_prompt(monkeypatch, db_session_factory):
    captured_prompts: list[str] = []

    async def fake_invoke_model(*, prompt, files, **_kwargs):
        captured_prompts.append(prompt)
        assert len(files) == 2
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr(
        "app.services.model_router.get_settings",
        lambda: Settings(
            _env_file=None,
            allow_insecure_defaults=True,
            rag_enabled=True,
            rag_context_max_chars=4000,
            model_context_window=20000,
            model_max_input_tokens=18000,
            model_chunk_max_chars=100000,
        ),
    )

    with db_session_factory() as db:
        task = _make_task()
        db.add(task)
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id))

    assert result.summary == "ok"
    assert captured_prompts
    assert "Evidence E1" in captured_prompts[0]
    assert "helper_copy" in captured_prompts[0]


def test_postprocess_review_result_removes_invalid_evidence_and_call_chain(db_session):
    from app.tasks.reviews import _postprocess_review_result

    task = _make_task()
    db_session.add(task)
    db_session.commit()
    build_rag_context(
        db_session,
        task,
        task.files,
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_context_max_chars=4000),
    )
    result = ModelReviewResponse(
        summary="ok",
        score=80,
        findings=[
            ReviewFinding(
                severity=FindingSeverity.HIGH,
                category=FindingCategory.MEMORY_SAFETY,
                title="bad evidence",
                description="bad evidence",
                file_path="src/driver.c",
                line=3,
                evidence_ids=["E1", "E999"],
                call_chain=["missing", "helper_copy"],
                confidence=0.9,
                remediation="fix it",
                code_snippet=[],
                fixed_snippet=[],
            )
        ],
    )

    postprocessed = _postprocess_review_result(task, result)

    assert postprocessed.findings[0].evidence_ids == ["E1"]
    assert postprocessed.findings[0].call_chain == []
