from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ModelNode, ReviewFile, ReviewTask, TaskStatus, User
from app.services.check_types import validate_check_types


ALLOWED_SOURCE_EXTENSIONS = {".c", ".h"}
SOURCE_TEXT_ENCODINGS = ("gb18030", "gbk", "big5", "cp950")
FALLBACK_SOURCE_TEXT_ENCODINGS = ("cp1252", "latin-1")
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


@dataclass
class _SourceCollection:
    label: str
    settings: Settings
    files: list[SubmittedFile] = field(default_factory=list)
    seen_paths: set[str] = field(default_factory=set)
    total_source_bytes: int = 0
    has_source_content: bool = False

    def remember_path(self, relative_path: str) -> None:
        if relative_path in self.seen_paths:
            raise SubmissionError(f"{self.label} contains duplicate paths")
        self.seen_paths.add(relative_path)

    def add_source_file(self, relative_path: str, content: bytes, declared_size: int | None = None) -> None:
        if PurePosixPath(relative_path).suffix.lower() not in ALLOWED_SOURCE_EXTENSIONS:
            return

        size_bytes = declared_size if declared_size is not None else len(content)
        _require_size_limit(size_bytes, self.settings)
        self.total_source_bytes += size_bytes
        if self.total_source_bytes > self.settings.upload_max_extracted_bytes:
            raise SubmissionError(f"{self.label} total extracted size exceeds limit")
        if self.total_source_bytes > self.settings.review_max_source_bytes:
            raise SubmissionError(f"{self.label} review source size exceeds limit")
        if len(self.files) >= self.settings.upload_max_files:
            raise SubmissionError(f"{self.label} contains too many source files")

        _require_size_limit(len(content), self.settings)
        try:
            source_text = _decode_source(content)
        except SubmissionError as exc:
            raise SubmissionError(f"{relative_path}: {exc}") from exc
        self.has_source_content = self.has_source_content or bool(source_text.strip())
        self.files.append(
            SubmittedFile(
                relative_path=relative_path,
                source_text=source_text,
                size_bytes=len(content),
            )
        )

    def to_submission(self, input_mode: str, display_name: str) -> Submission:
        if not self.files:
            raise SubmissionError(f"{self.label} contains no C source files")
        if not self.has_source_content:
            raise SubmissionError(f"{self.label} source files must not all be empty")
        return Submission(input_mode=input_mode, display_name=display_name, files=self.files)


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


def _looks_like_c_source_text(value: str) -> bool:
    if "\x00" in value:
        return False
    stripped = value.strip()
    if not stripped:
        return True

    allowed_controls = {"\t", "\n", "\r", "\f"}
    bad_controls = sum(1 for char in stripped if ord(char) < 32 and char not in allowed_controls)
    if bad_controls:
        return False

    printable = sum(1 for char in stripped if char.isprintable() or char in allowed_controls)
    if printable / max(len(stripped), 1) < 0.92:
        return False

    lowered = stripped.lower()
    source_markers = (
        "#include",
        "#define",
        "#pragma",
        "int ",
        "void ",
        "char ",
        "return",
        "typedef",
        "struct",
        "enum",
        "/*",
        "//",
        "{",
        "}",
        ";",
    )
    return any(marker in lowered for marker in source_markers)


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

    utf8_decoded = _decode_without_loss(content, "utf-8")
    if utf8_decoded is not None and _suspicious_mojibake_score(utf8_decoded) == 0:
        return utf8_decoded

    if _looks_like_utf16(content, little_endian=True):
        decoded = _decode_without_loss(content, "utf-16-le")
        if decoded is not None:
            return decoded
    if _looks_like_utf16(content, little_endian=False):
        decoded = _decode_without_loss(content, "utf-16-be")
        if decoded is not None:
            return decoded

    candidates: list[tuple[tuple[int, int, int, int], int, str]] = []
    if utf8_decoded is not None:
        candidates.append((_decoded_text_score(utf8_decoded), 1, utf8_decoded))
    for index, encoding in enumerate(SOURCE_TEXT_ENCODINGS):
        decoded = _decode_without_loss(content, encoding)
        if decoded is None:
            continue
        candidates.append((_decoded_text_score(decoded), -index, decoded))
    if candidates:
        best = max(candidates, key=lambda item: (item[0], item[1]))
        return best[2]

    for encoding in FALLBACK_SOURCE_TEXT_ENCODINGS:
        decoded = _decode_without_loss(content, encoding)
        if decoded is not None and _looks_like_c_source_text(decoded):
            return decoded

    normalized_guess = _charset_normalizer_guess(content)
    if normalized_guess is not None and _looks_like_c_source_text(normalized_guess):
        return normalized_guess

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

    collection = _SourceCollection("archive", settings)
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
                collection.remember_path(relative_path)
                if PurePosixPath(relative_path).suffix.lower() not in ALLOWED_SOURCE_EXTENSIONS:
                    continue
                extracted = archive.read(info)
                collection.add_source_file(relative_path, extracted, declared_size=info.file_size)
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

    return collection.to_submission("archive", filename)


def collect_folder_submission(files: list[tuple[str, bytes]], settings: Settings) -> Submission:
    if not files:
        raise SubmissionError("folder submission contains no files")

    collection = _SourceCollection("folder", settings)
    root_name = "selected-folder"

    for index, (filename, content) in enumerate(files, start=1):
        if index > settings.upload_max_archive_entries:
            raise SubmissionError("folder contains too many entries")
        relative_path = _safe_archive_path(filename, settings)
        collection.remember_path(relative_path)
        if PurePosixPath(relative_path).suffix.lower() not in ALLOWED_SOURCE_EXTENSIONS:
            continue
        if "/" in relative_path:
            root_name = relative_path.split("/", 1)[0] or root_name
        collection.add_source_file(relative_path, content)

    return collection.to_submission("folder", root_name)


def _node_min_gpu_index(node: ModelNode) -> int:
    return min(node.gpu_indices or [9999])


def _task_is_small_enough(settings: Settings, *, file_count: int, source_bytes: int) -> bool:
    return file_count <= settings.model_small_task_max_files and source_bytes <= settings.model_small_task_max_bytes


def _reserved_small_task_nodes(nodes: list[ModelNode], settings: Settings) -> set[str]:
    if len(nodes) < 3 or settings.model_small_task_reserved_nodes <= 0:
        return set()
    sorted_nodes = sorted(nodes, key=lambda node: (_node_min_gpu_index(node), node.created_at, node.id))
    return {node.id for node in sorted_nodes[-settings.model_small_task_reserved_nodes:]}


def _preferred_nodes_for_submission(
    nodes: list[ModelNode],
    settings: Settings,
    submission: Submission,
) -> list[ModelNode]:
    reserved_ids = _reserved_small_task_nodes(nodes, settings)
    if not reserved_ids:
        return nodes
    source_bytes = sum(source.size_bytes for source in submission.files)
    is_small = _task_is_small_enough(
        settings,
        file_count=len(submission.files),
        source_bytes=source_bytes,
    )
    if is_small:
        reserved = [node for node in nodes if node.id in reserved_ids]
        return reserved or nodes
    general = [node for node in nodes if node.id not in reserved_ids]
    if not general:
        return nodes
    return general[:settings.model_large_task_max_nodes]


def _select_model_node_for_review(db: Session, requested_node: ModelNode, submission: Submission) -> ModelNode:
    sibling_nodes = list(
        db.scalars(
            select(ModelNode).where(
                ModelNode.is_enabled.is_(True),
                ModelNode.model_identifier == requested_node.model_identifier,
                ModelNode.api_key == requested_node.api_key,
            )
        ).all()
    )
    if len(sibling_nodes) <= 1:
        return requested_node
    settings = get_settings()
    sibling_nodes.sort(key=lambda node: (_node_min_gpu_index(node), node.created_at, node.id))
    candidate_nodes = _preferred_nodes_for_submission(sibling_nodes, settings, submission)

    load_rows = db.execute(
        select(ReviewTask.model_node_id, func.count(ReviewTask.id))
        .where(ReviewTask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]))
        .group_by(ReviewTask.model_node_id)
    ).all()
    loads = {model_node_id: count for model_node_id, count in load_rows}
    candidate_nodes.sort(
        key=lambda node: (
            loads.get(node.id, 0),
            0 if node.id == requested_node.id else 1,
            _node_min_gpu_index(node),
            node.created_at,
        )
    )
    return candidate_nodes[0]


def create_review_task(
    db: Session,
    *,
    owner: User,
    model_node_id: str,
    submission: Submission,
    check_types: list[str],
    display_name: str | None = None,
) -> ReviewTask:
    model_node = db.get(ModelNode, model_node_id)
    if model_node is None or not model_node.is_enabled:
        raise SubmissionError("model node does not exist or is disabled")
    model_node = _select_model_node_for_review(db, model_node, submission)

    try:
        normalized_check_types = validate_check_types(check_types)
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc

    task = ReviewTask(
        owner=owner,
        model_node=model_node,
        input_mode=submission.input_mode,
        display_name=(display_name or "").strip() or submission.display_name,
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
