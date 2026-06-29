import asyncio
import json
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import CodeChunk, CodeEdge, CodeEmbedding, CodeParseCache, CodeProject, CodeSymbol, ModelNode, ReviewContext, ReviewFile, ReviewTask, User
from app.schemas.model_response import FindingCategory, FindingSeverity, ReviewFinding
from app.schemas.model_response import ModelReviewResponse
from app.services.code_index.context_builder import build_rag_context, render_rag_context
from app.services.code_index.evaluator import (
    GoldRetrievalCase,
    evaluate_evidence_quality,
    evaluate_finding_quality,
    evaluate_gold_cases,
    evaluate_graph_quality,
    evaluate_retrieval,
    measure_latency,
)
from app.services.code_index.indexer import build_code_index
from app.services.code_index.keyword_search import expand_query_terms, keyword_search_chunks
from app.services.code_index.parser import ParsedFile, ParsedSymbol, parse_c_source
from app.services.code_index.planner import plan_review_units
from app.services.code_index.retriever import RetrievedContext, _is_low_value_rag_identifier, _locally_defined_symbols, _prune_ranked_contexts, _qdrant_contexts, _vector_contexts, retrieve_context_diagnostics, retrieve_context_for_files, retrieve_missing_symbol_contexts
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


def test_parse_c_source_extracts_common_function_definition_styles():
    source = """
static inline int same_line(int value) { return value; }

char *pointer_return(const char *input)
{
    return (char *)input;
}

RT_WEAK rt_err_t driver_style(
        struct rt_device *dev,
        int flags)
{
    return rt_device_register(dev, "demo", flags);
}
"""

    parsed = parse_c_source("src/styles.c", source)
    function_names = {symbol.name for symbol in parsed.symbols if symbol.kind == "function"}

    assert {"same_line", "pointer_return", "driver_style"} <= function_names
    assert any(call.caller_name == "driver_style" and call.callee_name == "rt_device_register" for call in parsed.calls)


def test_parse_c_source_merges_optional_libclang_semantic_symbols(monkeypatch):
    def fake_libclang(relative_path: str, source_text: str) -> ParsedFile:
        return ParsedFile(
            relative_path=relative_path,
            line_count=1,
            symbols=[
                ParsedSymbol(
                    kind="declaration",
                    name="semantic_only",
                    signature="int semantic_only(void);",
                    start_line=1,
                    end_line=1,
                    confidence=0.96,
                    source_tool="libclang",
                )
            ],
        )

    monkeypatch.setattr("app.services.code_index.parser._libclang_file", fake_libclang)

    parsed = parse_c_source("include/semantic.h", "int normal_decl(void);\n")

    assert {symbol.name for symbol in parsed.symbols} >= {"semantic_only", "normal_decl"}
    assert any(symbol.source_tool == "libclang" for symbol in parsed.symbols)


def test_same_named_function_prefers_related_file_definition(db_session):
    user = User(username="same-name-user", password_hash=hash_password("pw"))
    node = ModelNode(display_name="RAG node", model_identifier="review-model", base_url="http://model-node", is_enabled=True)
    task = ReviewTask(
        owner=user,
        model_node=node,
        input_mode="folder",
        display_name="same-name",
        file_count=3,
        check_types=["memory_safety"],
    )
    task.files.extend(
        [
            ReviewFile(
                relative_path="drivers/foo.c",
                source_text='int init_device(void);\nint run(void) { return init_device(); }\n',
                size_bytes=64,
            ),
            ReviewFile(
                relative_path="drivers/foo_helpers.c",
                source_text="int init_device(void) { return 1; }\n",
                size_bytes=36,
            ),
            ReviewFile(
                relative_path="net/foo_helpers.c",
                source_text="int init_device(void) { return 2; }\n",
                size_bytes=36,
            ),
        ]
    )
    db_session.add(task)
    db_session.commit()

    build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    edge = db_session.query(CodeEdge).filter_by(edge_type="FUNCTION_CALLS_FUNCTION").one()
    target = db_session.get(CodeSymbol, edge.target_id)
    contexts = retrieve_context_for_files(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=5),
    )

    assert target is not None
    assert target.file.relative_path == "drivers/foo_helpers.c"
    assert contexts[0].file_path == "drivers/foo_helpers.c"


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


def test_definition_profile_omits_current_target_function_blocks(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    context = build_rag_context(
        db_session,
        task,
        task.files,
        settings=Settings(
            _env_file=None,
            allow_insecure_defaults=True,
            rag_retrieval_profile="definition",
            rag_context_format="segmented",
            rag_context_max_chars=4000,
        ),
    )

    assert "REFERENCE CONTEXT" in context
    assert "int helper_copy(int value)" not in context
    assert "buf[value]" not in context


def test_symbol_card_context_format_avoids_code_fences(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    context = build_rag_context(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(
            _env_file=None,
            allow_insecure_defaults=True,
            rag_retrieval_profile="definition",
            rag_context_format="cards",
            rag_context_max_chars=4000,
        ),
    )

    assert "SYMBOL CARD" in context
    assert "```c" not in context
    assert "===== FILE" not in context


def test_missing_symbol_context_resolves_calls_macros_and_globals(db_session):
    task = _make_task()
    task.files.append(
        ReviewFile(
            relative_path="src/state.c",
            source_text="int shared_counter = 7;\n",
            size_bytes=24,
        )
    )
    task.files[0].source_text += "int read_counter(void) { return shared_counter + MAX_PACKET_SIZE; }\n"
    db_session.add(task)
    db_session.commit()

    contexts = retrieve_missing_symbol_contexts(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=10),
    )
    rendered, selected = render_rag_context(contexts, max_chars=5000)

    assert selected
    assert "missing" in rendered
    assert "helper_copy" in rendered
    assert "shared_counter" in rendered


def test_parser_does_not_index_local_variables_or_struct_members_as_globals():
    source = """
struct Image { int width; };
int shared_counter = 7;
int inspect(struct Image *img) {
    int size1 = img->width + 1;
    char *buffer = 0;
    return size1 + (buffer != 0);
}
"""

    parsed = parse_c_source("src/scope.c", source)
    global_names = {symbol.name for symbol in parsed.symbols if symbol.kind == "global_variable"}

    assert "shared_counter" in global_names
    assert "size1" not in global_names
    assert "buffer" not in global_names
    assert "width" not in global_names


def test_local_declarations_are_not_treated_as_missing_symbols():
    source = """
int inspect(int width) {
    int size1 = width + 1;
    char *buffer = 0;
    return size1 + (buffer != 0);
}
"""

    locally_defined = _locally_defined_symbols(source)

    assert {"inspect", "width", "size1", "buffer"}.issubset(locally_defined)


def test_default_rag_query_filters_low_value_embedded_identifiers():
    assert _is_low_value_rag_identifier("CAN")
    assert _is_low_value_rag_identifier("CAN_TSR_RQCP0")
    assert _is_low_value_rag_identifier("IS_CAN_MODE")
    assert _is_low_value_rag_identifier("uint32_t")
    assert not _is_low_value_rag_identifier("helper_copy")


def test_invoke_selected_model_adds_definition_context_to_first_stage(monkeypatch, db_session_factory):
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

    assert result.summary == "两阶段审查完成，未输出符合类型要求的问题。"
    assert captured_prompts
    assert "Definition Context" in captured_prompts[0] or "DEFINITION CONTEXT" in captured_prompts[0]
    assert "MAX_PACKET_SIZE" in captured_prompts[0]


def test_chunked_first_stage_adds_batch_specific_definition_context(monkeypatch, db_session_factory):
    captured_prompts: list[str] = []

    async def fake_invoke_model(*, prompt, files, **_kwargs):
        captured_prompts.append(prompt)
        return ModelReviewResponse(summary="ok", score=100, findings=[])

    monkeypatch.setattr("app.services.model_router.invoke_model", fake_invoke_model)
    monkeypatch.setattr("app.services.model_router._should_chunk", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "app.services.model_router.get_settings",
        lambda: Settings(
            _env_file=None,
            allow_insecure_defaults=True,
            rag_enabled=True,
            rag_review_units_enabled=False,
            rag_context_max_chars=5000,
            model_context_window=20000,
            model_max_input_tokens=18000,
            model_chunk_max_chars=1000,
            model_chunk_max_count=20,
        ),
    )

    with db_session_factory() as db:
        task = _make_task()
        db.add(task)
        db.commit()
        task_id = task.id

    with db_session_factory() as db:
        result = asyncio.run(invoke_selected_model(db, task_id))

    assert "两阶段审查完成" in result.summary
    assert captured_prompts
    assert any("Definition Context" in prompt or "DEFINITION CONTEXT" in prompt for prompt in captured_prompts)
    assert any("helper_copy" in prompt or "MAX_PACKET_SIZE" in prompt for prompt in captured_prompts)


def test_postprocess_review_result_accepts_strict_report_fields(db_session):
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
            )
        ],
    )

    postprocessed = _postprocess_review_result(task, result)

    assert postprocessed.findings[0].title == "bad evidence"
    assert set(postprocessed.findings[0].model_dump()) == {
        "severity", "category", "title", "description", "file_path", "line"
    }


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


def test_function_units_group_related_function_chunks(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()
    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))

    units = plan_review_units(project)
    driver_unit = next(unit for unit in units if unit.unit_type == "function" and unit.symbol_name == "driver_entry")

    assert len(driver_unit.chunk_ids) >= 2


def test_parser_indexes_function_pointers_conditionals_and_callback_edges(db_session):
    user = User(username="callback-user", password_hash=hash_password("pw"))
    node = ModelNode(display_name="RAG node", model_identifier="review-model", base_url="http://model-node", is_enabled=True)
    task = ReviewTask(
        owner=user,
        model_node=node,
        input_mode="folder",
        display_name="callbacks",
        file_count=1,
        check_types=["memory_safety"],
    )
    task.files.append(
        ReviewFile(
            relative_path="src/callbacks.c",
            source_text=(
                "#ifdef USE_CALLBACK\n"
                "typedef int (*event_cb)(int value);\n"
                "static event_cb global_cb;\n"
                "int invoke_cb(int value) {\n"
                "    if (global_cb) return global_cb(value);\n"
                "    return value;\n"
                "}\n"
                "#endif\n"
            ),
            size_bytes=180,
        )
    )
    db_session.add(task)
    db_session.commit()

    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    kinds = {symbol.kind for symbol in db_session.query(CodeSymbol).filter_by(project_id=project.id).all()}
    edge_types = {edge.edge_type for edge in db_session.query(CodeEdge).filter_by(project_id=project.id).all()}

    assert "function_pointer" in kinds
    assert "conditional" in kinds
    assert "FUNCTION_USES_CALLBACK" in edge_types
    assert "FUNCTION_DEPENDS_ON_CONDITION" in edge_types


def test_parser_indexes_struct_callback_bindings(db_session):
    user = User(username="ops-user", password_hash=hash_password("pw"))
    node = ModelNode(display_name="RAG node", model_identifier="review-model", base_url="http://model-node", is_enabled=True)
    task = ReviewTask(
        owner=user,
        model_node=node,
        input_mode="folder",
        display_name="ops",
        file_count=1,
        check_types=["memory_safety"],
    )
    task.files.append(
        ReviewFile(
            relative_path="drivers/ops.c",
            source_text=(
                "static int driver_open(void) { return 0; }\n"
                "static const struct file_ops ops = {\n"
                "    .open = driver_open,\n"
                "};\n"
            ),
            size_bytes=120,
        )
    )
    db_session.add(task)
    db_session.commit()

    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    assert db_session.query(CodeSymbol).filter_by(project_id=project.id, kind="callback_binding", name="driver_open").one()
    edge = db_session.query(CodeEdge).filter_by(project_id=project.id, edge_type="CALLBACK_BINDING_TARGETS_FUNCTION").one()
    target = db_session.get(CodeSymbol, edge.target_id)
    assert target is not None
    assert target.name == "driver_open"


def test_parse_cache_reuses_same_source_across_tasks(monkeypatch):
    from app.services.code_index import parser as parser_module

    parser_module._parse_c_source_cached.cache_clear()
    call_count = 0
    original_builtin = parser_module._parse_c_source_builtin

    def counted_builtin(relative_path: str, source_text: str):
        nonlocal call_count
        call_count += 1
        return original_builtin(relative_path, source_text)

    monkeypatch.setattr(parser_module, "_tree_sitter_file", lambda _path, _text: None)
    monkeypatch.setattr(parser_module, "_libclang_file", lambda _path, _text: None)
    monkeypatch.setattr(parser_module, "_parse_c_source_builtin", counted_builtin)

    source = "int cached_fn(void) { return 1; }\n"
    first = parser_module.parse_c_source("src/cache.c", source)
    second = parser_module.parse_c_source("src/cache.c", source)

    assert first.symbols[0].name == second.symbols[0].name
    assert call_count == 1


def test_persistent_parse_cache_reuses_symbols_across_tasks(db_session):
    first = _make_task()
    second = _make_task()
    second.owner.username = "rag-user-2"
    db_session.add_all([first, second])
    db_session.commit()

    first_project = build_code_index(db_session, first, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    second_project = build_code_index(db_session, second, settings=Settings(_env_file=None, allow_insecure_defaults=True))

    assert first_project.stats_json["parse_cache_misses"] >= 1
    assert second_project.stats_json["parse_cache_hits"] >= 1
    assert db_session.query(CodeParseCache).count() >= 1


def test_negative_retrieval_samples_are_not_matched(db_session):
    user = User(username="negative-user", password_hash=hash_password("pw"))
    node = ModelNode(display_name="RAG node", model_identifier="review-model", base_url="http://model-node", is_enabled=True)
    task = ReviewTask(
        owner=user,
        model_node=node,
        input_mode="folder",
        display_name="negative",
        file_count=2,
        check_types=["memory_safety"],
    )
    task.files.extend(
        [
            ReviewFile(
                relative_path="src/target.c",
                source_text="int safe_target(int value) { return value + 1; }\n",
                size_bytes=48,
            ),
            ReviewFile(
                relative_path="net/unrelated.c",
                source_text="int dangerous_unrelated(void) { char buf[4]; return buf[99]; }\n",
                size_bytes=68,
            ),
        ]
    )
    db_session.add(task)
    db_session.commit()

    retrieved = retrieve_context_for_files(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=10),
    )
    result = evaluate_retrieval(retrieved, set(), k=10, must_not_retrieve={"net/unrelated.c:dangerous_unrelated"})

    assert result.negative_hit_rate == 0


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
    assert all(context.reason == "vector" for context in contexts)


def test_qdrant_contexts_use_payload_chunk_ids(monkeypatch, db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()
    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))
    helper_chunk = db_session.query(CodeChunk).filter_by(project_id=project.id, symbol_name="helper_copy", chunk_kind="function").one()

    class FakeQdrantClient:
        enabled = True

        def __init__(self, _settings):
            pass

        def search_sync(self, *_args, **_kwargs):
            return [{"score": 0.95, "payload": {"chunk_id": helper_chunk.id}}]

    monkeypatch.setattr("app.services.code_index.retriever.QdrantCodeIndexClient", FakeQdrantClient)

    contexts = _qdrant_contexts(
        db_session,
        project,
        task.files[0].source_text,
        {task.files[0].relative_path},
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_qdrant_url="http://qdrant"),
        limit=5,
    )

    assert contexts
    assert contexts[0].reason == "qdrant"
    assert contexts[0].symbol_name == "helper_copy"


def test_keyword_search_uses_query_expansion_and_bm25(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()
    project = build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))

    expanded = expand_query_terms({"helperCopy", "MAX_PACKET_SIZE"})
    hits = keyword_search_chunks(db_session, project, expanded, limit=10)

    assert {"helpercopy", "helper", "copy", "max_packet_size", "packet", "size"} & expanded
    assert hits
    assert any("bm25" in hit.reason or "symbol-exact" in hit.reason for hit in hits)
    assert any(hit.chunk.symbol_name == "helper_copy" for hit in hits)


def test_render_rag_context_deduplicates_and_allocates_budget():
    duplicate_low = RetrievedContext(
        chunk_id="chunk-1",
        evidence_id="a.c:1:5:foo",
        file_path="a.c",
        symbol_name="foo",
        start_line=1,
        end_line=5,
        content="int foo(void) { return bar(); }",
        reason="call:d1",
        score=1.0,
    )
    duplicate_high = RetrievedContext(
        chunk_id="chunk-1",
        evidence_id="a.c:1:5:foo",
        file_path="a.c",
        symbol_name="foo",
        start_line=1,
        end_line=5,
        content="int foo(void) { return bar(); }",
        reason="call:d1",
        score=3.0,
    )
    symbol_context = RetrievedContext(
        chunk_id="chunk-2",
        evidence_id="types.h:3:3:cfg",
        file_path="types.h",
        symbol_name="cfg",
        start_line=3,
        end_line=3,
        content="struct cfg { int limit; };",
        reason="symbol:function_uses_type",
        score=2.0,
    )

    rendered, selected = render_rag_context([duplicate_low, duplicate_high, symbol_context], max_chars=1200)

    assert rendered.count("[Evidence") == 2
    assert [context.score for context in selected] == [3.0, 2.0]
    assert "RAG Evidence Context" in rendered


def test_review_unit_payloads_prepare_function_units(db_session):
    from app.services.model_router import _rag_review_unit_files

    task = _make_task()
    db_session.add(task)
    db_session.commit()

    unit_files = _rag_review_unit_files(
        db_session,
        task,
        Settings(_env_file=None, allow_insecure_defaults=True, rag_review_units_enabled=True),
    )

    assert unit_files
    assert any("REVIEW UNIT" in item.source_text and "driver_entry" in item.source_text for item in unit_files)
    assert all(not item.relative_path.startswith("review-unit/") for item in unit_files)


def test_retrieve_context_diagnostics_reports_pruned_evidence(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    diagnostics = retrieve_context_diagnostics(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=10),
    )

    assert diagnostics["enabled"] is True
    assert diagnostics["selected_count"] >= 1
    assert diagnostics["raw_candidate_count"] >= diagnostics["selected_count"]
    assert "budget" in diagnostics


def test_prune_ranked_contexts_prefers_evidence_diversity():
    contexts = [
        RetrievedContext("c1", "E-call-1", "driver.c", "process", 10, 40, "call one", "call:d1", 9.0),
        RetrievedContext("c2", "E-call-2", "driver.c", "process", 12, 42, "call two", "call:d1", 8.9),
        RetrievedContext("c3", "E-symbol", "types.h", "Image", 1, 20, "symbol", "symbol:function_uses_type", 8.8),
        RetrievedContext("c4", "E-upstream", "main.c", "main", 1, 12, "upstream", "upstream", 8.7),
        RetrievedContext("c5", "E-keyword", "alloc.c", "alloc_image", 60, 80, "keyword", "keyword:bm25", 8.6),
        RetrievedContext("c6", "E-qdrant", "math.c", "size_calc", 100, 120, "qdrant", "qdrant", 8.5),
        RetrievedContext("c7", "E-missing", "free.c", "release", 140, 160, "missing", "missing:keyword", 8.4),
    ]

    selected = _prune_ranked_contexts(contexts, limit=8)
    reasons = {context.reason.split(":", 1)[0] for context in selected}

    assert len(selected) == 6
    assert reasons >= {"call", "symbol", "upstream", "keyword", "qdrant", "missing"}
    assert [context.evidence_id for context in selected].count("E-call-1") == 1


def test_gold_evaluator_aggregates_manual_fixture_metrics(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()

    retrieved = retrieve_context_for_files(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=10),
    )
    fixture_root = Path(__file__).parent / "fixtures" / "rag_projects"
    metadata = json.loads((fixture_root / "simple_call" / "metadata.json").read_text(encoding="utf-8"))
    summary = evaluate_gold_cases(
        [
            GoldRetrievalCase(
                name=metadata["case_id"],
                retrieved=retrieved,
                must_retrieve=set(metadata["must_retrieve"]),
                must_not_retrieve=set(metadata["must_not_retrieve"]),
            )
        ],
        k=10,
    )

    assert summary.case_count == 1
    assert summary.recall_at_k > 0
    assert 0 <= summary.precision_at_k <= 1
    assert 0 <= summary.ndcg_at_k <= 1
    assert 0 <= summary.token_waste_ratio <= 1
    assert summary.negative_hit_rate == 0


def test_extended_rag_metrics_cover_evidence_graph_findings_and_latency(db_session):
    task = _make_task()
    db_session.add(task)
    db_session.commit()
    build_code_index(db_session, task, settings=Settings(_env_file=None, allow_insecure_defaults=True))

    retrieved = retrieve_context_for_files(
        db_session,
        task,
        [task.files[0]],
        settings=Settings(_env_file=None, allow_insecure_defaults=True, rag_keyword_top_k=10),
    )
    retrieval_result = evaluate_retrieval(retrieved, {"src/helpers.c:helper_copy"}, k=10)
    rendered, selected = render_rag_context(retrieved, max_chars=4000)
    evidence_result = evaluate_evidence_quality(
        {"src/helpers.c:helper_copy"},
        selected,
        {"E1"},
    )
    edges = [
        (edge.edge_type, edge.metadata_json.get("callee_name") or edge.metadata_json.get("symbol_name", ""), target.name if target else None)
        for edge in db_session.query(CodeEdge).all()
        for target in [db_session.get(CodeSymbol, edge.target_id) if edge.target_id else None]
    ]
    graph_result = evaluate_graph_quality(
        edges,
        {("helper_copy", "helper_copy")},
        {("helper_copy", "helper_copy")},
    )
    finding_result = evaluate_finding_quality(
        {"memory_safety:src/helpers.c:3"},
        {"memory_safety:src/helpers.c:3", "input_validation:src/driver.c:3"},
    )
    latency = measure_latency([1.0, 3.0, 2.0, 10.0, 5.0])

    assert rendered
    assert retrieval_result.ndcg_at_k > 0
    assert evidence_result.evidence_coverage == 1.0
    assert 0 <= evidence_result.citation_accuracy <= 1
    assert 0 <= graph_result.call_edge_accuracy <= 1
    assert 0 <= graph_result.declaration_definition_match_rate <= 1
    assert finding_result.finding_precision == 1.0
    assert finding_result.finding_recall == 0.5
    assert latency.p95_ms >= latency.p50_ms
