from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ReviewContext, ReviewEvidence, ReviewFile, ReviewTask
from app.services.code_index.retriever import (
    RetrievedContext,
    retrieve_context_for_files,
    retrieve_missing_symbol_contexts,
)


def build_rag_context(
    db: Session,
    task: ReviewTask,
    files: list[ReviewFile],
    *,
    settings: Settings | None = None,
    persist: bool = True,
) -> str:
    settings = settings or get_settings()
    if not settings.rag_enabled:
        return ""
    contexts = [
        *retrieve_missing_symbol_contexts(db, task, files, settings=settings),
        *retrieve_context_for_files(db, task, files, settings=settings),
    ]
    rendered, selected = render_rag_context(contexts, max_chars=settings.rag_context_max_chars)
    if persist and rendered:
        _persist_review_context(db, task, rendered, selected)
    return rendered


def render_rag_context(contexts: list[RetrievedContext], *, max_chars: int) -> tuple[str, list[RetrievedContext]]:
    if not contexts:
        return "", []
    intro = [
        "RAG Evidence Context:",
        "Use Current Target first, then cite Evidence E numbers only when they directly support the finding.",
        "Missing-symbol evidence resolves calls/macros/types/globals that are not defined in the current target.",
        "Prefer call/declaration/macro/type/global evidence over weak keyword or vector-only evidence.",
    ]
    contexts = _select_budgeted_contexts(_dedupe_contexts(contexts), max_chars=max_chars - len("\n".join(intro)))
    rendered = list(intro)
    used = len("\n".join(rendered))
    selected: list[RetrievedContext] = []
    for index, context in enumerate(contexts, start=1):
        header = (
            f"\n[Evidence E{index}] {context.reason} score={context.score:.2f} "
            f"{context.file_path}:{context.start_line}-{context.end_line} "
            f"{context.symbol_name or ''}".rstrip()
        )
        content = _trim_context_content(context)
        block = f"{header}\n```c\n{content}\n```"
        if used + len(block) > max_chars:
            continue
        rendered.append(block)
        selected.append(context)
        used += len(block)
    return "\n".join(rendered), selected


def _dedupe_contexts(contexts: list[RetrievedContext]) -> list[RetrievedContext]:
    best_by_key: dict[str, RetrievedContext] = {}
    for context in contexts:
        key = context.chunk_id or context.evidence_id
        current = best_by_key.get(key)
        if current is None or context.score > current.score:
            best_by_key[key] = context
    return sorted(best_by_key.values(), key=lambda item: item.score, reverse=True)


def _select_budgeted_contexts(contexts: list[RetrievedContext], *, max_chars: int) -> list[RetrievedContext]:
    if max_chars <= 0:
        return []
    buckets: dict[str, list[RetrievedContext]] = defaultdict(list)
    for context in contexts:
        buckets[_budget_bucket(context)].append(context)

    bucket_budget = {
        "graph": int(max_chars * 0.60),
        "symbol": int(max_chars * 0.30),
        "search": int(max_chars * 0.10),
    }
    selected: list[RetrievedContext] = []
    deferred: list[RetrievedContext] = []
    used_total = 0
    for bucket_name in ("graph", "symbol", "search"):
        used_bucket = 0
        for context in buckets.get(bucket_name, []):
            cost = _context_render_cost(context)
            if used_bucket + cost <= bucket_budget[bucket_name] and used_total + cost <= max_chars:
                selected.append(context)
                used_bucket += cost
                used_total += cost
            else:
                deferred.append(context)

    for context in sorted(deferred, key=lambda item: item.score, reverse=True):
        cost = _context_render_cost(context)
        if used_total + cost <= max_chars:
            selected.append(context)
            used_total += cost
    return sorted(selected, key=lambda item: item.score, reverse=True)


def _budget_bucket(context: RetrievedContext) -> str:
    reason = context.reason.split(":", 1)[0]
    if reason in {"call", "include", "upstream"}:
        return "graph"
    if reason == "symbol":
        return "symbol"
    return "search"


def _context_render_cost(context: RetrievedContext) -> int:
    return len(_trim_context_content(context)) + 180


def _trim_context_content(context: RetrievedContext) -> str:
    bucket = _budget_bucket(context)
    limit = {"graph": 1300, "symbol": 760, "search": 320}.get(bucket, 320)
    if len(context.content) <= limit:
        return context.content
    return f"{context.content[:limit].rstrip()}\n/* ... evidence truncated by budget ... */"


def _persist_review_context(
    db: Session,
    task: ReviewTask,
    context_text: str,
    contexts: list[RetrievedContext],
) -> None:
    for existing in list(task.review_contexts):
        db.delete(existing)
    db.flush()
    review_context = ReviewContext(
        task=task,
        project_id=task.code_project.id if task.code_project is not None else None,
        context_text=context_text,
        token_estimate=max(1, len(context_text) // 4),
        metadata_json={
            "evidence_count": len(contexts),
            "dedupe_enabled": True,
            "budgeting": {"graph": 0.60, "symbol": 0.30, "search": 0.10},
        },
    )
    db.add(review_context)
    db.flush()
    for index, context in enumerate(contexts, start=1):
        db.add(
            ReviewEvidence(
                task_id=task.id,
                context=review_context,
                chunk_id=context.chunk_id,
                evidence_key=f"E{index}",
                file_path=context.file_path,
                symbol_name=context.symbol_name,
                start_line=context.start_line,
                end_line=context.end_line,
                reason=context.reason,
                score=context.score,
                metadata_json={"source_evidence_id": context.evidence_id},
            )
        )
    db.flush()
