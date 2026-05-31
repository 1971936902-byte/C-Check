from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic

from app.db.models import Report, ReviewTask, TaskStatus
from app.db.session import SessionLocal
from app.services.model_router import invoke_selected_model
from app.services.reports import build_report
from app.worker import celery_app


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def run_review_task(task_id: str) -> None:
    started = monotonic()
    with SessionLocal() as db:
        task = db.get(ReviewTask, task_id)
        if task is None:
            return
        task.status = TaskStatus.RUNNING
        task.progress = 10
        task.error_message = None
        task.started_at = datetime.now(UTC)
        if task.report is not None:
            db.delete(task.report)
        db.commit()

        try:
            result = asyncio.run(invoke_selected_model(db, task_id))
            task = db.get(ReviewTask, task_id)
            if task is None:
                return
            report = build_report(task, result)
            db.add(report)
            task.status = TaskStatus.COMPLETED
            task.progress = 100
            task.finding_count = len(result.findings)
            task.error_message = None
            task.duration_ms = _elapsed_ms(started)
            task.completed_at = datetime.now(UTC)
            db.commit()
        except Exception as exc:
            db.rollback()
            task = db.get(ReviewTask, task_id)
            if task is None:
                return
            stale_report = db.get(Report, task.report.id) if task.report is not None else None
            if stale_report is not None:
                db.delete(stale_report)
            task.status = TaskStatus.FAILED
            task.progress = 100
            task.finding_count = 0
            task.error_message = str(exc)[:1000] or exc.__class__.__name__
            task.duration_ms = _elapsed_ms(started)
            task.completed_at = datetime.now(UTC)
            db.commit()


@celery_app.task(name="app.tasks.reviews.dispatch_review")
def dispatch_review(task_id: str) -> None:
    run_review_task(task_id)
