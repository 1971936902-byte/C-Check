from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ReviewContext, ReviewEvidence, ReviewFile, ReviewTask
from app.services.code_index.retriever import RetrievedContext, retrieve_context_for_files


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
    contexts = retrieve_context_for_files(db, task, files, settings=settings)
    rendered, selected = render_rag_context(contexts, max_chars=settings.rag_context_max_chars)
    if persist and rendered:
        _persist_review_context(db, task, rendered, selected)
    return rendered


def render_rag_context(contexts: list[RetrievedContext], *, max_chars: int) -> tuple[str, list[RetrievedContext]]:
    if not contexts:
        return "", []
    rendered = [
        "RAG关联上下文：以下代码片段来自同一任务的符号索引、调用图和关键字检索。"
        "请优先基于 Current Target 和 Evidence Context 判断问题。"
        "如果风险依赖外部函数，请在 finding.evidence_ids 中引用对应 E 编号。"
    ]
    used = len(rendered[0])
    selected: list[RetrievedContext] = []
    for index, context in enumerate(contexts, start=1):
        header = (
            f"\n[Evidence E{index}] {context.reason} score={context.score:.2f} "
            f"{context.file_path}:{context.start_line}-{context.end_line} "
            f"{context.symbol_name or ''}".rstrip()
        )
        block = f"{header}\n```c\n{context.content}\n```"
        if used + len(block) > max_chars:
            break
        rendered.append(block)
        selected.append(context)
        used += len(block)
    return "\n".join(rendered), selected


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
        metadata_json={"evidence_count": len(contexts)},
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
