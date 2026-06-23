from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ReviewFile, ReviewTask
from app.services.code_index.retriever import RetrievedContext, retrieve_context_for_files


def build_rag_context(
    db: Session,
    task: ReviewTask,
    files: list[ReviewFile],
    *,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    if not settings.rag_enabled:
        return ""
    contexts = retrieve_context_for_files(db, task, files, settings=settings)
    return render_rag_context(contexts, max_chars=settings.rag_context_max_chars)


def render_rag_context(contexts: list[RetrievedContext], *, max_chars: int) -> str:
    if not contexts:
        return ""
    rendered = [
        "RAG关联上下文：以下代码片段来自同一任务的符号索引、调用图和关键字检索。"
        "仅将其作为定位被审查代码相关定义/调用关系的证据；不要把证据片段本身当作新的审查目标。"
    ]
    used = len(rendered[0])
    for index, context in enumerate(contexts, start=1):
        header = (
            f"\n[E{index}] {context.reason} score={context.score:.2f} "
            f"{context.file_path}:{context.start_line}-{context.end_line} "
            f"{context.symbol_name or ''}".rstrip()
        )
        block = f"{header}\n```c\n{context.content}\n```"
        if used + len(block) > max_chars:
            break
        rendered.append(block)
        used += len(block)
    return "\n".join(rendered)
