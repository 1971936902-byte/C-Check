from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ReviewContext, ReviewEvidence, ReviewFile, ReviewTask
from app.services.code_index.retriever import (
    RAG_PURPOSE_CANDIDATE,
    RAG_PURPOSE_CONFIRMATION,
    RAG_PURPOSE_DEFAULT,
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
    purpose: str = RAG_PURPOSE_DEFAULT,
) -> str:
    settings = settings or get_settings()
    if not settings.rag_enabled:
        return ""
    contexts = [
        *retrieve_missing_symbol_contexts(db, task, files, settings=settings, purpose=purpose),
        *retrieve_context_for_files(db, task, files, settings=settings, purpose=purpose),
    ]
    rendered, selected = render_rag_context(
        contexts,
        max_chars=_rag_max_chars(settings, purpose),
        context_format=settings.rag_context_format,
        purpose=purpose,
    )
    if persist and rendered:
        _persist_review_context(db, task, rendered, selected, purpose=purpose)
    return rendered


def render_rag_context(
    contexts: list[RetrievedContext],
    *,
    max_chars: int,
    context_format: str = "code",
    purpose: str = RAG_PURPOSE_DEFAULT,
) -> tuple[str, list[RetrievedContext]]:
    if not contexts:
        return "", []
    normalized_format = context_format.strip().lower()
    intro = _rag_intro(normalized_format, purpose)
    contexts = _select_budgeted_contexts(_dedupe_contexts(contexts), max_chars=max_chars - len("\n".join(intro)))
    rendered = list(intro)
    used = len("\n".join(rendered))
    selected: list[RetrievedContext] = []
    for index, context in enumerate(contexts, start=1):
        block = _render_evidence_block(index, context, normalized_format)
        if used + len(block) > max_chars:
            continue
        rendered.append(block)
        selected.append(context)
        used += len(block)
    return "\n".join(rendered), selected


def _rag_max_chars(settings: Settings, purpose: str) -> int:
    if purpose == RAG_PURPOSE_CANDIDATE:
        return min(settings.rag_context_max_chars, 2600)
    if purpose == RAG_PURPOSE_CONFIRMATION:
        return min(settings.rag_context_max_chars, 3200)
    return settings.rag_context_max_chars


def _rag_intro(context_format: str, purpose: str) -> list[str]:
    if context_format in {"segmented", "cards", "symbol_cards"}:
        if purpose in {RAG_PURPOSE_CANDIDATE, RAG_PURPOSE_CONFIRMATION}:
            return [
                "DEFINITION CONTEXT (RAG, auxiliary only):",
                "Use these cards only to resolve unknown structs, typedefs, enums, macros, globals, callbacks, and directly called functions.",
                "Do not treat this section as vulnerability evidence by itself.",
                "Judge risks from PRIMARY SOURCE first; use these cards only when a symbol or declaration is unclear.",
                "Every finding.file_path and finding.line must still point to the PRIMARY SOURCE.",
            ]
        return [
            "REFERENCE CONTEXT (RAG, auxiliary only):",
            "Use these Evidence E numbers only to understand declarations, types, macros, constants, and directly called functions that are not clear in PRIMARY SOURCE.",
            "Do not report a finding solely from REFERENCE CONTEXT.",
            "Every finding.file_path and finding.line must point to the PRIMARY SOURCE supplied in the user message, not to a reference-only location.",
            "If REFERENCE CONTEXT repeats code from PRIMARY SOURCE, treat PRIMARY SOURCE as authoritative and do not duplicate findings.",
        ]
    if purpose in {RAG_PURPOSE_CANDIDATE, RAG_PURPOSE_CONFIRMATION}:
        return [
            "Definition Context (RAG):",
            "Use PRIMARY SOURCE as the only audit target.",
            "Use the cards below only to understand unknown definitions or declarations.",
            "Do not report a vulnerability just because a card exists.",
            "Do not anchor findings to this section; anchor them to executable statements in PRIMARY SOURCE.",
        ]
    return [
        "RAG Evidence Context:",
        "Use Current Target first, then cite Evidence E numbers only when they directly support the finding.",
        "Missing-symbol evidence resolves calls/macros/types/globals that are not defined in the current target.",
        "Prefer call/declaration/macro/type/global evidence over weak keyword or vector-only evidence.",
    ]


def _render_evidence_block(index: int, context: RetrievedContext, context_format: str) -> str:
    header = (
        f"\n[Evidence E{index}] {context.reason} score={context.score:.2f} "
        f"{context.file_path}:{context.start_line}-{context.end_line} "
        f"{context.symbol_name or ''}".rstrip()
    )
    if context_format in {"cards", "symbol_cards"}:
        return f"{header}\n{_symbol_card(context)}"
    content = _trim_context_content(context)
    return f"{header}\n```c\n{content}\n```"


def _symbol_card(context: RetrievedContext) -> str:
    content = _trim_context_content(context)
    symbol = context.symbol_name or "(file)"
    kind = context.reason.split(":", 1)[-1]
    return "\n".join(
        [
            "SYMBOL CARD:",
            f"- kind: {kind}",
            f"- symbol: {symbol}",
            f"- location: {context.file_path}:{context.start_line}-{context.end_line}",
            "- reference:",
            _indent_reference(content),
        ]
    )


def _indent_reference(content: str) -> str:
    return "\n".join(f"  {line}" for line in content.splitlines()[:40])


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
    return sorted(selected, key=_evidence_render_order)


def _budget_bucket(context: RetrievedContext) -> str:
    reason = context.reason.split(":", 1)[0]
    if reason in {"call", "include", "upstream"}:
        return "graph"
    if reason == "symbol":
        return "symbol"
    return "search"


def _evidence_render_order(context: RetrievedContext) -> tuple[int, int, float]:
    span = max(0, context.end_line - context.start_line)
    precision_rank = 0 if span <= 12 else 1 if span <= 40 else 2
    bucket_rank = {
        "graph": 0,
        "symbol": 1,
        "search": 2,
    }.get(_budget_bucket(context), 3)
    return precision_rank, bucket_rank, -context.score


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
    *,
    purpose: str,
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
            "purpose": purpose,
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
