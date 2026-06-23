from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.services.code_index.parser import PARSER_VERSION, ParsedSymbol


CTAGS_SOURCE = "universal-ctags"


@dataclass(frozen=True)
class CtagsProbe:
    available: bool
    executable: str | None = None
    version: str | None = None
    error: str | None = None


def probe_ctags() -> CtagsProbe:
    executable = shutil.which("ctags")
    if executable is None:
        return CtagsProbe(available=False, error="ctags executable not found")
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CtagsProbe(available=False, executable=executable, error=str(exc))
    output = (completed.stdout or completed.stderr or "").splitlines()
    version = output[0].strip() if output else None
    return CtagsProbe(available=completed.returncode == 0, executable=executable, version=version)


def extract_ctags_symbols(relative_path: str, source_text: str, *, timeout_seconds: int = 5) -> list[ParsedSymbol]:
    executable = shutil.which("ctags")
    if executable is None:
        return []
    suffix = Path(relative_path).suffix or ".c"
    with tempfile.TemporaryDirectory(prefix="c-check-ctags-") as temp_dir:
        source_path = Path(temp_dir) / f"source{suffix}"
        source_path.write_text(source_text, encoding="utf-8", errors="ignore")
        command = [
            executable,
            "--output-format=json",
            "--fields=+neK",
            "-o",
            "-",
            str(source_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
    if completed.returncode != 0 or not completed.stdout:
        return []
    symbols: list[ParsedSymbol] = []
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        line_number = _safe_int(item.get("line")) or 1
        end_line = _safe_int(item.get("end")) or line_number
        kind = _normalize_kind(item.get("kind"))
        symbols.append(
            ParsedSymbol(
                kind=kind,
                name=name,
                signature=item.get("pattern") if isinstance(item.get("pattern"), str) else None,
                start_line=line_number,
                end_line=max(line_number, end_line),
                confidence=0.80,
                source_tool=CTAGS_SOURCE,
            )
        )
    return symbols


def _safe_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _normalize_kind(value: object) -> str:
    if not isinstance(value, str):
        return "symbol"
    normalized = value.lower()
    if normalized in {"function", "prototype"}:
        return "function" if normalized == "function" else "declaration"
    if normalized in {"macro", "define"}:
        return "macro"
    if normalized in {"struct", "union", "enum", "typedef"}:
        return normalized
    if normalized in {"variable", "member", "externvar"}:
        return "global_variable"
    return normalized or PARSER_VERSION
