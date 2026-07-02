from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Sequence

from app.core.config import Settings
from app.db.models import ReviewFile
from app.schemas.model_response import ReviewFinding


BASE_CHECKERS = ("core", "unix.Malloc", "unix.MismatchedDeallocator")
STREAM_CHECKER_PREFERENCE = ("unix.Stream", "alpha.unix.Stream", "alpha.unix.SimpleStream")


@dataclass(frozen=True)
class AnalyzerPathStep:
    file_path: str
    line: int
    column: int
    message: str


@dataclass(frozen=True)
class AnalyzerDiagnostic:
    checker: str
    category: str
    message: str
    file_path: str
    line: int
    column: int
    path: tuple[AnalyzerPathStep, ...] = ()


@dataclass
class ClangAnalysisResult:
    available: bool
    completed: bool
    diagnostics: list[AnalyzerDiagnostic] = field(default_factory=list)
    analyzed_files: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    executable: str | None = None


def run_clang_static_analysis(
    files: Sequence[ReviewFile],
    settings: Settings,
) -> ClangAnalysisResult:
    started = perf_counter()
    executable = shutil.which(settings.clang_static_analysis_executable)
    if executable is None:
        return ClangAnalysisResult(
            available=False,
            completed=False,
            errors=[f"{settings.clang_static_analysis_executable} executable not found"],
            elapsed_seconds=perf_counter() - started,
        )

    source_files = [source for source in files if source.relative_path.lower().endswith(".c")]
    if len(source_files) > settings.clang_static_analysis_max_files:
        source_files = source_files[: settings.clang_static_analysis_max_files]
    result = ClangAnalysisResult(available=True, completed=True, executable=executable)
    checker_argument = _checker_argument(executable)
    with tempfile.TemporaryDirectory(prefix="c-check-clang-") as temp_dir:
        root = Path(temp_dir)
        _materialize_sources(root, files)
        if settings.clang_static_analysis_ctu_enabled and len(source_files) > 1:
            ctu_result = _run_codechecker_ctu(root, source_files, executable, settings)
            if ctu_result is not None and ctu_result.completed:
                ctu_result.elapsed_seconds = perf_counter() - started
                return ctu_result
            if ctu_result is not None:
                result.errors.extend(ctu_result.errors)
        for index, source in enumerate(source_files):
            relative_path = _safe_relative_path(source.relative_path)
            source_path = root / relative_path
            output_path = root / f"clang-result-{index}.plist"
            command = [
                executable,
                "--analyze",
                "-x",
                "c",
                "-std=c11",
                "-I",
                str(root),
                "-Xanalyzer",
                f"-analyzer-checker={checker_argument}",
                "-Xanalyzer",
                "-analyzer-output=plist",
                str(source_path),
                "-o",
                str(output_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=settings.clang_static_analysis_timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                result.completed = False
                result.errors.append(f"{relative_path}: {exc}")
                continue
            stderr = completed.stderr.strip()
            if completed.returncode != 0:
                result.completed = False
                result.errors.append(f"{relative_path}: {stderr or f'exit {completed.returncode}'}")
                continue
            result.analyzed_files.add(relative_path.as_posix())
            if output_path.exists():
                try:
                    result.diagnostics.extend(_parse_plist(output_path, root))
                except (OSError, ValueError, plistlib.InvalidFileException) as exc:
                    result.completed = False
                    result.errors.append(f"{relative_path}: invalid plist: {exc}")
            elif stderr:
                result.errors.append(f"{relative_path}: {stderr}")
    result.elapsed_seconds = perf_counter() - started
    return result


def _run_codechecker_ctu(
    root: Path,
    source_files: Sequence[ReviewFile],
    clang_executable: str,
    settings: Settings,
) -> ClangAnalysisResult | None:
    codechecker = _resolve_codechecker(settings.codechecker_executable)
    if codechecker is None:
        return None
    compile_commands = root / "compile_commands.json"
    entries = []
    for source in source_files:
        relative_path = _safe_relative_path(source.relative_path)
        entries.append(
            {
                "directory": str(root),
                "arguments": [
                    clang_executable,
                    "-I",
                    str(root),
                    "-std=c11",
                    "-c",
                    str(root / relative_path),
                    "-o",
                    str(root / f"{relative_path.as_posix().replace('/', '_')}.o"),
                ],
                "file": str(root / relative_path),
            }
        )
    compile_commands.write_text(json.dumps(entries), encoding="utf-8")
    reports = root / "codechecker-reports"
    checker_argument = _checker_argument(clang_executable)
    enabled_checkers = [checker for checker in checker_argument.split(",") if checker != "core"]
    command = [
        codechecker,
        "analyze",
        str(compile_commands),
        "-o",
        str(reports),
        "--analyzers",
        "clangsa",
        "--ctu",
        "--ctu-ast-mode",
        "load-from-pch",
        "--jobs",
        "2",
    ]
    for checker in enabled_checkers:
        command.extend(["-e", checker])
    env = os.environ.copy()
    env["PATH"] = _llvm_tool_path(clang_executable, env.get("PATH", ""))
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=max(settings.clang_static_analysis_timeout_seconds, 120),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ClangAnalysisResult(
            available=True,
            completed=False,
            executable=codechecker,
            errors=[f"CodeChecker CTU failed: {exc}"],
        )
    if completed.returncode != 0:
        return ClangAnalysisResult(
            available=True,
            completed=False,
            executable=codechecker,
            errors=[f"CodeChecker CTU failed: {completed.stderr.strip() or completed.stdout.strip()}"],
        )
    result = ClangAnalysisResult(available=True, completed=True, executable=codechecker)
    result.analyzed_files = {_safe_relative_path(source.relative_path).as_posix() for source in source_files}
    for plist_path in reports.glob("*.plist"):
        try:
            result.diagnostics.extend(_parse_plist(plist_path, root))
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            result.completed = False
            result.errors.append(f"{plist_path.name}: invalid plist: {exc}")
    return result


def _resolve_codechecker(configured: str) -> str | None:
    executable = shutil.which(configured)
    if executable is not None:
        return executable
    sibling = Path(sys.executable).with_name(configured)
    return str(sibling) if sibling.exists() else None


def _llvm_tool_path(clang_executable: str, current_path: str) -> str:
    paths = [str(Path(clang_executable).resolve().parent)]
    name = Path(clang_executable).name
    version = name.rsplit("-", 1)[-1] if "-" in name else ""
    if version.isdigit():
        llvm_bin = Path(f"/usr/lib/llvm-{version}/bin")
        if llvm_bin.exists():
            paths.insert(0, str(llvm_bin))
    if current_path:
        paths.append(current_path)
    return os.pathsep.join(paths)


def diagnostics_to_findings(diagnostics: Sequence[AnalyzerDiagnostic]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for diagnostic in diagnostics:
        category, severity = _finding_classification(diagnostic)
        findings.append(
            ReviewFinding(
                severity=severity,
                category=category,
                title=_finding_title(diagnostic),
                description=diagnostic.message[:360],
                file_path=diagnostic.file_path,
                line=max(1, diagnostic.line),
            )
        )
    return findings


def _checker_argument(executable: str) -> str:
    available: set[str] = set()
    for option in ("-analyzer-checker-help", "-analyzer-checker-help-alpha"):
        try:
            completed = subprocess.run(
                [executable, "-cc1", option],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            parts = line.split()
            if parts:
                available.add(parts[0])
    selected = list(BASE_CHECKERS)
    stream_checker = next((checker for checker in STREAM_CHECKER_PREFERENCE if checker in available), None)
    if stream_checker is not None:
        selected.append(stream_checker)
    return ",".join(dict.fromkeys(selected))


def _materialize_sources(root: Path, files: Sequence[ReviewFile]) -> None:
    for source in files:
        relative_path = _safe_relative_path(source.relative_path)
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.source_text, encoding="utf-8")


def _safe_relative_path(value: str) -> Path:
    normalized = value.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", ".."}]
    return Path(*parts) if parts else Path("source.c")


def _parse_plist(path: Path, root: Path) -> list[AnalyzerDiagnostic]:
    payload = plistlib.loads(path.read_bytes())
    files = [str(item) for item in payload.get("files", [])]
    diagnostics: list[AnalyzerDiagnostic] = []
    for item in payload.get("diagnostics", []):
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        file_path = _plist_file_path(files, location.get("file"), root)
        path_steps: list[AnalyzerPathStep] = []
        for piece in item.get("path", []):
            if not isinstance(piece, dict):
                continue
            piece_location = piece.get("location") or piece.get("start") or {}
            path_steps.append(
                AnalyzerPathStep(
                    file_path=_plist_file_path(files, piece_location.get("file"), root),
                    line=int(piece_location.get("line") or 1),
                    column=int(piece_location.get("col") or 1),
                    message=str(piece.get("message") or piece.get("extended_message") or ""),
                )
            )
        diagnostics.append(
            AnalyzerDiagnostic(
                checker=str(item.get("check_name") or "clang-analyzer"),
                category=str(item.get("category") or "Logic error"),
                message=str(item.get("description") or "Clang Static Analyzer finding"),
                file_path=file_path,
                line=int(location.get("line") or 1),
                column=int(location.get("col") or 1),
                path=tuple(path_steps),
            )
        )
    return diagnostics


def _plist_file_path(files: list[str], index: object, root: Path) -> str:
    try:
        raw = Path(files[int(index)])
    except (IndexError, TypeError, ValueError):
        return "unknown.c"
    try:
        return raw.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return raw.as_posix()


def _finding_classification(diagnostic: AnalyzerDiagnostic) -> tuple[str, str]:
    text = f"{diagnostic.checker} {diagnostic.category} {diagnostic.message}".lower()
    if "leak" in text or "stream" in diagnostic.checker.lower():
        is_memory_leak = "malloc" in diagnostic.checker.lower() or "memory" in text
        return "resource_leak", "high" if is_memory_leak else "medium"
    if any(token in text for token in ("use-after-free", "double free", "released memory")):
        return "memory_safety", "high"
    if any(token in text for token in ("out of bound", "buffer overflow", "array bound")):
        return "buffer_overflow", "high"
    if "null" in text:
        return "pointer_safety", "medium"
    if any(token in text for token in ("divide", "overflow", "shift")):
        return "integer_safety", "medium"
    return "logic", "medium"


def _finding_title(diagnostic: AnalyzerDiagnostic) -> str:
    checker = diagnostic.checker.rsplit(".", 1)[-1]
    return f"Clang {checker}: {diagnostic.message}"[:120]
