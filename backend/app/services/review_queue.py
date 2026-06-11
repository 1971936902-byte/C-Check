from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ReviewTask, TaskStatus


def queued_order(statement):
    return statement.order_by(
        ReviewTask.queue_priority.desc(),
        ReviewTask.created_at.asc(),
        ReviewTask.id.asc(),
    )


def ordered_queued_tasks(db: Session) -> list[ReviewTask]:
    return list(
        db.scalars(
            queued_order(select(ReviewTask).where(ReviewTask.status == TaskStatus.QUEUED))
        ).all()
    )


def attach_queue_positions(db: Session, tasks: list[ReviewTask]) -> None:
    queued_ids = [task.id for task in ordered_queued_tasks(db)]
    positions = {task_id: index for index, task_id in enumerate(queued_ids)}
    for task in tasks:
        task.queued_ahead_count = positions.get(task.id) if task.status == TaskStatus.QUEUED else None


def _send_to_worker(task_id: str) -> None:
    from app.tasks.reviews import dispatch_review as celery_dispatch_review

    celery_dispatch_review.delay(task_id)


def dispatch_next_review(db: Session) -> ReviewTask | None:
    running_count = db.scalar(
        select(func.count()).select_from(ReviewTask).where(ReviewTask.status == TaskStatus.RUNNING)
    ) or 0
    if running_count:
        return None
    task = db.scalar(
        queued_order(select(ReviewTask).where(ReviewTask.status == TaskStatus.QUEUED).limit(1))
    )
    if task is None:
        return None
    try:
        task.status = TaskStatus.RUNNING
        task.progress = max(task.progress, 1)
        db.commit()
        _send_to_worker(task.id)
    except Exception as exc:
        db.rollback()
        failed = db.get(ReviewTask, task.id)
        if failed is not None:
            failed.status = TaskStatus.FAILED
            failed.progress = 100
            failed.error_message = f"failed to dispatch review worker: {exc}"[:1000]
            db.commit()
    return task


def pin_queued_task(db: Session, task: ReviewTask) -> ReviewTask:
    if task.status != TaskStatus.QUEUED:
        raise ValueError("only queued tasks can be pinned")
    max_priority = db.scalar(select(func.max(ReviewTask.queue_priority))) or 0
    task.queue_priority = max_priority + 1
    db.commit()
    db.refresh(task)
    dispatch_next_review(db)
    attach_queue_positions(db, [task])
    return task
