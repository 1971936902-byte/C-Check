from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class ClangdStatus:
    available: bool
    executable: str | None = None
    reason: str | None = None


def probe_clangd() -> ClangdStatus:
    executable = shutil.which("clangd")
    if executable is None:
        return ClangdStatus(available=False, reason="clangd executable not found")
    return ClangdStatus(available=True, executable=executable)
