from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ModelNode, ReviewFile, ReviewTask, User
from app.services.check_types import validate_check_types


ALLOWED_SOURCE_EXTENSIONS = {".c", ".h"}
SOURCE_TEXT_ENCODINGS = ("gb18030", "gbk", "big5", "cp950", "cp1252", "latin-1")
TEXT_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


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
    from app.services.review_queue import dispatch_next_review

    with SessionLocal() as db:
        dispatch_next_review(db)


def _cjk_score(value: str) -> int:
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _suspicious_mojibake_score(value: str) -> int:
    return sum(1 for char in value if "\u0300" <= char <= "\u05ff" or char == "\ufffd")


def _kana_score(value: str) -> int:
    return sum(1 for char in value if "\u3040" <= char <= "\u30ff")


def _decoded_text_score(value: str) -> tuple[int, int, int, int]:
    return (
        -value.count("\x00"),
        -_suspicious_mojibake_score(value),
        -_kana_score(value),
        _cjk_score(value),
    )


def _decode_without_loss(content: bytes, encoding: str) -> str | None:
    try:
        return content.decode(encoding)
    except (UnicodeDecodeError, UnicodeError):
        return None


def _charset_normalizer_guess(content: bytes) -> str | None:
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        return None

    match = from_bytes(content).best()
    if match is None or match.encoding is None:
        return None
    return _decode_without_loss(content, match.encoding)


def _looks_like_utf16(content: bytes, *, little_endian: bool) -> bool:
    if len(content) < 4:
        return False
    nul_bytes = content[1::2] if little_endian else content[0::2]
    return nul_bytes.count(0) > max(2, len(content) // 8)


def _decode_source(content: bytes) -> str:
    for marker, encoding in TEXT_BOMS:
        if content.startswith(marker):
            decoded = _decode_without_loss(content, encoding)
            if decoded is not None:
                return decoded

    decoded = _decode_without_loss(content, "utf-8")
    if decoded is not None:
        return decoded

    if _looks_like_utf16(content, little_endian=True):
        decoded = _decode_without_loss(content, "utf-16-le")
        if decoded is not None:
            return decoded
    if _looks_like_utf16(content, little_endian=False):
        decoded = _decode_without_loss(content, "utf-16-be")
        if decoded is not None:
            return decoded

    decoded = _charset_normalizer_guess(content)
    if decoded is not None:
        return decoded

    candidates: list[tuple[tuple[int, int, int, int], int, str]] = []
    for index, encoding in enumerate(SOURCE_TEXT_ENCODINGS):
        decoded = _decode_without_loss(content, encoding)
        if decoded is None:
            continue
        candidates.append((_decoded_text_score(decoded), -index, decoded))
    if candidates:
        return max(candidates)[2]
    raise SubmissionError("source files must use a supported text encoding")


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


def collect_folder_submission(files: list[tuple[str, bytes]], settings: Settings) -> Submission:
    if not files:
        raise SubmissionError("folder submission contains no files")

    submitted_files: list[SubmittedFile] = []
    total_size = 0
    seen_paths: set[str] = set()
    has_source_content = False
    root_name = "selected-folder"

    for index, (filename, content) in enumerate(files, start=1):
        if index > settings.upload_max_archive_entries:
            raise SubmissionError("folder contains too many entries")
        relative_path = _safe_archive_path(filename, settings)
        if relative_path in seen_paths:
            raise SubmissionError("folder contains duplicate paths")
        seen_paths.add(relative_path)
        if PurePosixPath(relative_path).suffix.lower() not in ALLOWED_SOURCE_EXTENSIONS:
            continue
        if "/" in relative_path:
            root_name = relative_path.split("/", 1)[0] or root_name
        _require_size_limit(len(content), settings)
        total_size += len(content)
        if total_size > settings.upload_max_extracted_bytes:
            raise SubmissionError("folder total source size exceeds limit")
        if len(submitted_files) >= settings.upload_max_files:
            raise SubmissionError("folder contains too many source files")
        source_text = _decode_source(content)
        has_source_content = has_source_content or bool(source_text.strip())
        submitted_files.append(
            SubmittedFile(
                relative_path=relative_path,
                source_text=source_text,
                size_bytes=len(content),
            )
        )

    if not submitted_files:
        raise SubmissionError("folder contains no C source files")
    if not has_source_content:
        raise SubmissionError("folder source files must not all be empty")
    return Submission(input_mode="folder", display_name=root_name, files=submitted_files)


def create_review_task(
    db: Session,
    *,
    owner: User,
    model_node_id: str,
    submission: Submission,
    check_types: list[str],
) -> ReviewTask:
    model_node = db.get(ModelNode, model_node_id)
    if model_node is None or not model_node.is_enabled:
        raise SubmissionError("model node does not exist or is disabled")

    try:
        normalized_check_types = validate_check_types(check_types)
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc

    task = ReviewTask(
        owner=owner,
        model_node=model_node,
        input_mode=submission.input_mode,
        display_name=submission.display_name,
        file_count=len(submission.files),
        check_types=normalized_check_types,
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
