from __future__ import annotations

import logging

from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.models import ReviewTask
from app.db.session import SessionLocal
from app.services.code_index.indexer import build_code_index
from app.services.code_index.retriever import retrieve_context_diagnostics
from app.worker import celery_app


def run_graph_build(task_id: str, build_id: str) -> None:
    with SessionLocal() as db:
        try:
            task = db.scalar(select(ReviewTask).where(ReviewTask.id == task_id).with_for_update())
            if task is None or task.graph_build_id != build_id or task.graph_build_status not in {"queued", "running"}:
                return
            task.graph_build_status = "running"
            db.commit()
            # Serialize duplicate deliveries; a redelivered interrupted build can resume.
            task = db.scalar(select(ReviewTask).where(ReviewTask.id == task_id).with_for_update().execution_options(populate_existing=True))
            if task is None or task.graph_build_id != build_id or task.graph_build_status != "running":
                return
            settings = get_settings().model_copy(update={"rag_retrieval_profile": "balanced", "rag_on_demand_enabled": False})
            project = build_code_index(db, task, settings=settings)
            # Generate display diagnostics without replacing the review's evidence.
            diagnostics = retrieve_context_diagnostics(db, task, list(task.files), settings=settings)
            # Do not embed stats inside themselves. GET endpoints only read this snapshot.
            project.stats_json = {**project.stats_json, "observability": {
                key: value for key, value in diagnostics.items() if key not in {"stats", "project_id"}
            }}
            task.graph_build_status = "completed"
            task.graph_build_error = None
            db.commit()
        except Exception:
            db.rollback()
            logging.getLogger(__name__).exception("Graph build failed for task %s", task_id)
            db.execute(update(ReviewTask).where(
                ReviewTask.id == task_id, ReviewTask.graph_build_id == build_id,
                ReviewTask.graph_build_status.in_(["queued", "running"]),
            ).values(graph_build_status="failed", graph_build_error="图谱生成失败，请重试或查看 worker 日志。"))
            db.commit()


@celery_app.task(name="app.tasks.code_index.dispatch_graph_build")
def dispatch_graph_build(task_id: str, build_id: str) -> None:
    run_graph_build(task_id, build_id)
