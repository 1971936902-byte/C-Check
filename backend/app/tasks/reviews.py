from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic

from app.core.config import get_settings
from app.db.models import Report, ReviewTask, TaskStatus
from app.db.session import SessionLocal
from app.schemas.model_response import ModelReviewResponse
from app.services.model_router import ModelInvocationError, invoke_selected_model, truncate_model_log
from app.services.reports import build_report
from app.worker import celery_app


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _append_model_log(current: str | None, entry: str) -> str:
    timestamp = datetime.now(UTC).isoformat()
    combined = "\n\n".join(part for part in [current, f"[{timestamp}] {entry}"] if part)
    return truncate_model_log(combined) or ""


def _failure_log(attempt: int, exc: Exception) -> str:
    parts = [f"Attempt {attempt} failed: {exc}"]
    if isinstance(exc, ModelInvocationError):
        if exc.details:
            parts.append(f"Details:\n{exc.details}")
        if exc.raw_response:
            parts.append(f"Raw model response:\n{truncate_model_log(exc.raw_response, 6000)}")
    return "\n".join(parts)


def _is_structured_output_audit_failure(exc: Exception) -> bool:
    return isinstance(exc, ModelInvocationError) and str(exc) == "model returned an invalid structured response"


def _retry_instruction(attempt: int, exc: Exception) -> str:
    if _is_structured_output_audit_failure(exc):
        parts = [
            "The previous model output backend JSON schema audit failed.",
            "Return exactly one complete JSON object only. Do not include Markdown, comments, prose, or extra keys.",
            "The object must contain exactly: summary, score, findings. Every finding must match the required enum values and field types.",
            "Return at most 8 findings and escape all quotes/newlines as valid JSON strings.",
            f"Audit failure from attempt {attempt}:",
        ]
        if isinstance(exc, ModelInvocationError):
            if exc.details:
                parts.append(f"Validation details:\n{exc.details}")
            if exc.raw_response:
                parts.append(f"Rejected raw response excerpt:\n{truncate_model_log(exc.raw_response, 3000)}")
        return truncate_model_log("\n\n".join(parts), 4000) or ""
    return truncate_model_log(_failure_log(attempt, exc), 4000) or ""


def _invoke_with_retries(db, task_id: str, max_attempts: int) -> ModelReviewResponse:
    last_exc: Exception | None = None
    retry_instruction: str | None = None
    for attempt in range(1, max_attempts + 1):
        task = db.get(ReviewTask, task_id)
        if task is None:
            raise ModelInvocationError("review task does not exist")
        task.model_log = _append_model_log(task.model_log, f"Attempt {attempt} started.")
        db.commit()
        try:
            result = asyncio.run(invoke_selected_model(db, task_id, retry_instruction=retry_instruction))
        except Exception as exc:
            db.rollback()
            last_exc = exc
            task = db.get(ReviewTask, task_id)
            if task is None:
                raise
            task.model_log = _append_model_log(task.model_log, _failure_log(attempt, exc))
            retry_instruction = _retry_instruction(attempt, exc)
            if attempt < max_attempts:
                task.progress = min(90, 10 + attempt * 25)
                task.error_message = f"{exc}; retrying ({attempt}/{max_attempts})"[:1000]
            db.commit()
            continue

        task = db.get(ReviewTask, task_id)
        if task is not None:
            task.model_log = _append_model_log(
                task.model_log,
                f"Attempt {attempt} succeeded with {len(result.findings)} finding(s).",
            )
            db.commit()
        return result
    assert last_exc is not None
    task = db.get(ReviewTask, task_id)
    if task is not None and _is_structured_output_audit_failure(last_exc):
        task.model_log = _append_model_log(
            task.model_log,
            f"Final audit result: failed after {max_attempts} attempt(s).",
        )
        db.commit()
        raise ModelInvocationError(
            f"model output audit failed after {max_attempts} attempts",
            details=retry_instruction,
        ) from last_exc
    raise last_exc


def run_review_task(task_id: str) -> None:
    started = monotonic()
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(ReviewTask, task_id)
        if task is None:
            return
        task.status = TaskStatus.RUNNING
        task.progress = 10
        task.error_message = None
        task.model_log = None
        task.started_at = datetime.now(UTC)
        if task.report is not None:
            db.delete(task.report)
        db.commit()

        try:
            result = _invoke_with_retries(db, task_id, settings.model_max_attempts)
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
