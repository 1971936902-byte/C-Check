from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Report, ReviewTask, TaskStatus
from app.db.session import SessionLocal
from app.schemas.model_response import FindingCategory, FindingSeverity, ModelReviewResponse, ReviewFinding
from app.services.model_router import ModelInvocationError, invoke_selected_model, truncate_model_log
from app.services.reports import populate_report
from app.worker import celery_app


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _append_model_log(current: str | None, entry: str) -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
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


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./")


def _source_file_for_finding(task: ReviewTask, file_path: str):
    wanted = _normalized_path(file_path)
    if not wanted:
        return None
    files = list(task.files)
    for source in files:
        if _normalized_path(source.relative_path) == wanted:
            return source
    suffix_matches = [source for source in files if _normalized_path(source.relative_path).endswith(f"/{wanted}")]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    basename_matches = [source for source in files if _normalized_path(source.relative_path).split("/")[-1] == wanted.split("/")[-1]]
    if len(basename_matches) == 1:
        return basename_matches[0]
    return None


DATA_ONLY_LINE_PATTERN = re.compile(r"^[\s{},().+\-*/&|^~!?:<>=0-9xXa-fA-FuUlL'\"]+$")
ACTIONABLE_C_ANCHOR_PATTERN = re.compile(
    r"\b("
    r"if|for|while|switch|case|return|goto|break|continue|sizeof|"
    r"malloc|calloc|realloc|free|memcpy|memmove|memset|strcpy|strncpy|"
    r"strcat|strncat|sprintf|snprintf|scanf|fscanf|sscanf|fgets|gets|"
    r"open|fopen|close|fclose|read|write|recv|send|lock|unlock"
    r")\b|[A-Za-z_]\w*\s*\(|[A-Za-z_]\w*\s*(?:->|\.)[A-Za-z_]\w*|"
    r"[A-Za-z_]\w*\s*(?:\[[^\]]+\]\s*)?(?:=|\+=|-=|\*=|/=|%=|<<=|>>=|&=|\|=|\^=|\+\+|--)"
)


def _is_comment_only_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("//") or stripped.startswith("/*") or stripped.endswith("*/")


def _line_has_actionable_c_anchor(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return True
    if _is_comment_only_line(stripped):
        return False
    if DATA_ONLY_LINE_PATTERN.match(stripped):
        return False
    return bool(ACTIONABLE_C_ANCHOR_PATTERN.search(stripped))


def _finding_has_actionable_anchor(task: ReviewTask, file_path: str, line_number: int | None) -> bool:
    if line_number is None:
        return True
    source = _source_file_for_finding(task, file_path)
    if source is None:
        return True
    lines = source.source_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False
    return _line_has_actionable_c_anchor(lines[line_number - 1])


def _nearest_actionable_line(task: ReviewTask, finding: ReviewFinding, *, radius: int = 4) -> int | None:
    if finding.line is None:
        return None
    source = _source_file_for_finding(task, finding.file_path)
    if source is None:
        return None
    lines = source.source_text.splitlines()
    if finding.line < 1 or finding.line > len(lines):
        return None
    original_line = lines[finding.line - 1]
    if _line_has_actionable_c_anchor(original_line):
        return finding.line
    if DATA_ONLY_LINE_PATTERN.match(original_line.strip()):
        return None

    start = max(1, finding.line - radius)
    end = min(len(lines), finding.line + radius)
    for distance in range(1, radius + 1):
        candidates = [finding.line + distance, finding.line - distance]
        for candidate in candidates:
            if candidate < start or candidate > end:
                continue
            if _line_has_actionable_c_anchor(lines[candidate - 1]):
                return candidate
    return None


def _normalize_finding_anchors(task: ReviewTask, result: ModelReviewResponse) -> ModelReviewResponse:
    normalized = []
    changed = False
    for finding in result.findings:
        if _finding_has_actionable_anchor(task, finding.file_path, finding.line):
            normalized.append(finding)
            continue
        anchor = _nearest_actionable_line(task, finding)
        if anchor is None:
            normalized.append(finding)
            continue
        normalized.append(finding.model_copy(update={"line": anchor}))
        changed = True
    return result.model_copy(update={"findings": normalized}) if changed else result


def _filter_unanchored_findings(task: ReviewTask, result: ModelReviewResponse) -> ModelReviewResponse:
    kept = [
        finding
        for finding in result.findings
        if _finding_has_actionable_anchor(task, finding.file_path, finding.line)
    ]
    if len(kept) == len(result.findings):
        return result
    if kept:
        summary = f"已过滤无法定位到有效代码语句的误报，保留 {len(kept)} 个问题。"
    else:
        summary = "经源码行定位校验，未发现可定位到有效代码语句的问题。"
    return result.model_copy(update={"summary": summary, "findings": kept})


NULL_POINTER_TEXT_PATTERN = re.compile(r"(空指针|null\s*pointer|null|nil)", re.IGNORECASE)
PERIPHERAL_REGISTER_ACCESS_PATTERN = re.compile(
    r"\b(?:CAN|DAC|DMA\d?|CRC|DBGMCU|RCC|GPIO[A-Z]?|USART\d?|UART\d?|SPI\d?|I2C\d?|TIM\d?|ADC\d?)\s*->"
)
def _finding_text(finding: ReviewFinding) -> str:
    return " ".join(part for part in [finding.title, finding.description] if part)


def _finding_source_line(task: ReviewTask, finding: ReviewFinding) -> str:
    if finding.line is None:
        return ""
    source = _source_file_for_finding(task, finding.file_path)
    if source is None:
        return ""
    lines = source.source_text.splitlines()
    if finding.line < 1 or finding.line > len(lines):
        return ""
    return lines[finding.line - 1]


def _is_null_pointer_finding(finding: ReviewFinding) -> bool:
    return bool(NULL_POINTER_TEXT_PATTERN.search(_finding_text(finding)))


def _is_peripheral_register_null_pointer_false_positive(task: ReviewTask, finding: ReviewFinding) -> bool:
    source_line = _finding_source_line(task, finding)
    combined = "\n".join([source_line, _finding_text(finding)])
    return bool(PERIPHERAL_REGISTER_ACCESS_PATTERN.search(combined)) and _is_null_pointer_finding(finding)


def _downgrade_null_pointer_findings(task: ReviewTask, result: ModelReviewResponse) -> ModelReviewResponse:
    changed = False
    findings: list[ReviewFinding] = []
    for finding in result.findings:
        if not _is_null_pointer_finding(finding):
            findings.append(finding)
            continue
        if finding.severity == FindingSeverity.SUGGESTION:
            findings.append(finding)
            continue
        if finding.category in {
            FindingCategory.POINTER_SAFETY,
            FindingCategory.MEMORY_SAFETY,
            FindingCategory.INPUT_VALIDATION,
        }:
            description = finding.description
            if _is_peripheral_register_null_pointer_false_positive(task, finding):
                description = "嵌入式外设寄存器通常是固定映射地址，空指针风险需结合平台头文件确认。"
            findings.append(
                finding.model_copy(
                    update={
                        "severity": FindingSeverity.SUGGESTION,
                        "category": FindingCategory.MAINTAINABILITY,
                        "description": description,
                    }
                )
            )
            changed = True
            continue
        findings.append(finding)
    return result.model_copy(update={"findings": findings}) if changed else result


def _postprocess_review_result(task: ReviewTask, result: ModelReviewResponse) -> ModelReviewResponse:
    return _downgrade_null_pointer_findings(
        task,
        _filter_unanchored_findings(task, _normalize_finding_anchors(task, result)),
    )


def _retry_instruction(attempt: int, exc: Exception) -> str:
    if _is_structured_output_audit_failure(exc):
        parts = [
            "The previous model output backend JSON schema audit failed.",
            "Return exactly one smaller complete JSON object only. Do not include Markdown, comments, prose, or extra keys.",
            "The object must contain exactly: summary, score, findings. Every finding must match the required enum values and field types.",
            "Keep descriptions short. Escape all quotes/newlines as valid JSON strings.",
            "Internally scan integer, bounds, lifetime, leak, and exhaustion categories before selecting findings.",
            f"Audit failure from attempt {attempt}:",
        ]
        if isinstance(exc, ModelInvocationError):
            if exc.details:
                parts.append(f"Validation details:\n{exc.details}")
            if exc.raw_response:
                parts.append(f"Raw model response:\n{truncate_model_log(exc.raw_response, 3000)}")
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
    try:
        with SessionLocal() as db:
            task = db.get(ReviewTask, task_id)
            if task is None:
                return
            task.status = TaskStatus.RUNNING
            task.progress = 10
            task.error_message = None
            task.model_log = None
            task.candidate_jsonl = None
            task.started_at = datetime.now(timezone.utc)
            if task.report is not None:
                db.delete(task.report)
            db.commit()

            try:
                result = _invoke_with_retries(db, task_id, settings.model_max_attempts)
                task = db.get(ReviewTask, task_id)
                if task is None:
                    return
                result = _postprocess_review_result(task, result)
                existing_report = db.scalar(select(Report).where(Report.task_id == task.id))
                report = populate_report(existing_report or Report(task=task), task, result)
                db.add(report)
                db.flush()
                task.status = TaskStatus.COMPLETED
                task.progress = 100
                task.finding_count = len(result.findings)
                task.error_message = None
                task.duration_ms = _elapsed_ms(started)
                task.completed_at = datetime.now(timezone.utc)
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
                task.completed_at = datetime.now(timezone.utc)
                db.commit()
    finally:
        from app.services.review_queue import dispatch_next_review

        with SessionLocal() as db:
            dispatch_next_review(db)


@celery_app.task(name="app.tasks.reviews.dispatch_review")
def dispatch_review(task_id: str) -> None:
    run_review_task(task_id)
