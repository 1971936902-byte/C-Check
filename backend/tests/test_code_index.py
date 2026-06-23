import asyncio
from time import perf_counter

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import CodeChunk, CodeEdge, CodeEmbedding, CodeProject, CodeSymbol, ModelNode, ReviewContext, ReviewFile, ReviewTask, User
from app.schemas.model_response import FindingCategory, FindingSeverity, ReviewFinding
from app.schemas.model_response import ModelReviewResponse
from app.services.code_index.context_builder import build_rag_context
from app.services.code_index.evaluator import evaluate_retrieval
from app.services.code_index.indexer import build_code_index
from app.services.code_index.parser import parse_c_source
from app.services.code_index.planner import plan_review_units
from app.services.code_index.retriever import _vector_contexts, retrieve_context_for_files
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
        file_count=4,
        check_types=["memory_safety"],
    )
    task.files.extend(
        [
            ReviewFile(
                relative_path="src/driver.c",
                source_text=(
                    '#include "helpers.h"\n'
                    '#include "config.h"\n'
                    "int driver_entry(int value) {\n"
                    "    if (value > MAX_PACKET_SIZE) return -1;\n"
                    "    return helper_copy(value);\n"
                    "}\n"
                ),
                size_bytes=88,
            ),
            ReviewFile(
                relative_path="include/helpers.h",
                source_text="int helper_copy(int value);\n",
                size_bytes=24,
            ),
            ReviewFile(
                relative_path="include/config.h",
                source_text="#define MAX_PACKET_SIZE 64\n",
                size_bytes=27,
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


def test_parse_c_source_handles_driver_style_file_without_regex_backtracking():
    declarations = "\n".join(
        f"static const struct rt_device_ops ops_{index} = {{ 0 }};"
        for index in range(220)
    )
    source = (
        '#include "rtdevice.h"\n'
        "#define DRIVER_LIMIT 32\n"
        f"{declarations}\n"
        "static int rt_driver_probe(struct rt_device *dev) {\n"
        "    if (dev == 0) return -1;\n"
        "    return rt_device_register(dev, \"demo\", DRIVER_LIMIT);\n"
        "}\n"
    )

    started = perf_counter()
    parsed = parse_c_source("drivers/demo.c", source)
    elapsed = perf_counter() - started

    assert elapsed < 1.0
    assert {symbol.name for symbol in parsed.symbols} >= {"DRIVER_LIMIT", "rt_driver_probe"}
    assert any(call.callee_name == "rt_device_register" for call in parsed.calls)


def test_build_code_index_persists_graph_entities(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    db_session.commit()

    assert db_session.query(CodeProject).count() == 1
    assert project.stats_json["files"] == 4
    assert db_session.query(CodeSymbol).filter_by(name="helper_copy", kind="function").one()
    assert db_session.query(CodeChunk).filter_by(chunk_kind="file_summary").count() == 4
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
        assert len(files) == 4
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


def test_code_index_builds_extended_edges_embeddings_and_review_units(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))

    edge_types = {edge.edge_type for edge in db_session.query(CodeEdge).all()}
    assert "CALLSITE_CALLS_SYMBOL" in edge_types
    assert "SYMBOL_DECLARED_IN" in edge_types
    assert "SYMBOL_DEFINED_IN" in edge_types
    assert "FUNCTION_USES_MACRO" in edge_types
    assert db_session.query(CodeEmbedding).count() >= db_session.query(CodeChunk).count()
    units = plan_review_units(project)
    assert any(unit.unit_type == "function" and unit.symbol_name == "driver_entry" for unit in units)
    assert any(unit.unit_type == "callsite" for unit in units)


def test_rag_evaluator_reports_recall_precision_and_mrr(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    retrieved = retrieve_context_for_files(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=10),
    )
    result = evaluate_retrieval(retrieved, {"helper_copy"}, k=10)

    assert result.recall_at_k > 0
    assert result.precision_at_k > 0
    assert result.mrr > 0
    assert 0 <= result.token_waste_ratio <= 1


def test_vector_contexts_keep_positive_similarity_candidates(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()
    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))

    contexts = _vector_contexts(db_session, project, task.files[0].source_text, {task.files[0].relative_path}, limit=10)

    assert contexts
    assert all(context.reason == "向量相似检索" for context in contexts)
