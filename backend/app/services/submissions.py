from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ModelNode, ReviewFile, ReviewTask, User


ALLOWED_SOURCE_EXTENSIONS = {".c", ".h"}


class SubmissionError(ValueError):
    """Raised when submitted source code does not meet upload rules."""


@dataclass(frozen=True)
class SubmittedFile:
    relative_path: str
    source_text: str
    size_bytes: int


@dataclass(frozen=True)
class Submission:
    input_mode: str
    display_name: str
    files: list[SubmittedFile]


def dispatch_review(task_id: str) -> None:
    from app.db.session import SessionLocal
    from app.db.models import TaskStatus
    from app.tasks.reviews import dispatch_review as celery_dispatch_review

    try:
        celery_dispatch_review.delay(task_id)
    except Exception as exc:
        with SessionLocal() as db:
            task = db.get(ReviewTask, task_id)
            if task is None:
                return
            task.status = TaskStatus.FAILED
            task.progress = 100
            task.error_message = f"failed to dispatch review worker: {exc}"[:1000]
            db.commit()


def _decode_source(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SubmissionError("source files must use UTF-8 encoding") from exc


def _require_source_extension(filename: str) -> None:
    if PurePosixPath(filename).suffix.lower() not in ALLOWED_SOURCE_EXTENSIONS:
        raise SubmissionError("only .c and .h source file extensions are allowed")


def _require_size_limit(size_bytes: int, settings: Settings) -> None:
    if size_bytes > settings.upload_max_file_bytes:
        raise SubmissionError("source file exceeds size limit")


def _safe_archive_path(filename: str, settings: Settings) -> str:
    normalized = filename.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(filename)
    if (
        not normalized
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        raise SubmissionError("archive contains unsafe path")
    if len(normalized) > settings.upload_max_path_length:
        raise SubmissionError("archive path is too long")
    return posix_path.as_posix()


def collect_text_submission(source_text: str, settings: Settings | None = None) -> Submission:
    if not source_text.strip():
        raise SubmissionError("source text must not be empty")
    try:
        encoded = source_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SubmissionError("source text must use valid UTF-8 characters") from exc
    if settings is not None:
        _require_size_limit(len(encoded), settings)
    return Submission(
        input_mode="text",
        display_name="snippet.c",
        files=[SubmittedFile(relative_path="snippet.c", source_text=source_text, size_bytes=len(encoded))],
    )


def collect_file_submission(filename: str, content: bytes, settings: Settings) -> Submission:
    safe_name = _safe_archive_path(filename, settings)
    if "/" in safe_name:
        raise SubmissionError("source filename must not contain a path")
    if not safe_name or safe_name == ".":
        raise SubmissionError("source filename must not be empty")
    _require_source_extension(safe_name)
    _require_size_limit(len(content), settings)
    source_text = _decode_source(content)
    if not source_text.strip():
        raise SubmissionError("source file must not be empty")
    return Submission(
        input_mode="file",
        display_name=safe_name,
        files=[SubmittedFile(relative_path=safe_name, source_text=source_text, size_bytes=len(content))],
    )


def collect_archive_submission(filename: str, content: bytes, settings: Settings) -> Submission:
    if PurePosixPath(filename).suffix.lower() != ".zip":
        raise SubmissionError("archive must use .zip extension")
    if len(content) > settings.upload_max_archive_bytes:
        raise SubmissionError("zip archive exceeds upload size limit")

    submitted_files: list[SubmittedFile] = []
    total_size = 0
    seen_paths: set[str] = set()
    has_source_content = False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for entry_count, info in enumerate(archive.infolist(), start=1):
                if entry_count > settings.upload_max_archive_entries:
                    raise SubmissionError("archive contains too many entries")
                relative_path = _safe_archive_path(info.filename, settings)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise SubmissionError("archive symbolic links are not allowed")
                if info.is_dir():
                    continue
                if relative_path in seen_paths:
                    raise SubmissionError("archive contains duplicate paths")
                seen_paths.add(relative_path)
                if PurePosixPath(relative_path).suffix.lower() not in ALLOWED_SOURCE_EXTENSIONS:
                    continue
                _require_size_limit(info.file_size, settings)
                total_size += info.file_size
                if total_size > settings.upload_max_extracted_bytes:
                    raise SubmissionError("archive total extracted size exceeds limit")
                if len(submitted_files) >= settings.upload_max_files:
                    raise SubmissionError("archive contains too many source files")

                extracted = archive.read(info)
                _require_size_limit(len(extracted), settings)
                source_text = _decode_source(extracted)
                has_source_content = has_source_content or bool(source_text.strip())
                submitted_files.append(
                    SubmittedFile(
                        relative_path=relative_path,
                        source_text=source_text,
                        size_bytes=len(extracted),
                    )
                )
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        RuntimeError,
        NotImplementedError,
        UnicodeDecodeError,
        UnicodeEncodeError,
    ) as exc:
        raise SubmissionError("invalid zip archive") from exc

    if not submitted_files:
        raise SubmissionError("archive contains no C source files")
    if not has_source_content:
        raise SubmissionError("archive source files must not all be empty")
    return Submission(input_mode="archive", display_name=filename, files=submitted_files)


def create_review_task(
    db: Session,
    *,
    owner: User,
    model_node_id: str,
    submission: Submission,
) -> ReviewTask:
    model_node = db.get(ModelNode, model_node_id)
    if model_node is None or not model_node.is_enabled:
        raise SubmissionError("model node does not exist or is disabled")

    task = ReviewTask(
        owner=owner,
        model_node=model_node,
        input_mode=submission.input_mode,
        display_name=submission.display_name,
        file_count=len(submission.files),
    )
    task.files.extend(
        ReviewFile(
            relative_path=source.relative_path,
            source_text=source.source_text,
            size_bytes=source.size_bytes,
        )
        for source in submission.files
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    dispatch_review(task.id)
    db.refresh(task)
    return task
