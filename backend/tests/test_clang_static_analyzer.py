from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.models import ReviewFile
from app.services.code_index.clang_static_analyzer import (
    diagnostics_to_findings,
    run_clang_static_analysis,
)


def _settings(**updates) -> Settings:
    return Settings(
        _env_file=None,
        allow_insecure_defaults=True,
        clang_static_analysis_ctu_enabled=False,
        **updates,
    )


def test_clang_adapter_degrades_when_executable_is_missing(monkeypatch):
    monkeypatch.setattr("app.services.code_index.clang_static_analyzer.shutil.which", lambda _name: None)

    result = run_clang_static_analysis(
        [ReviewFile(relative_path="main.c", source_text="int main(void) { return 0; }", size_bytes=28)],
        _settings(),
    )

    assert result.available is False
    assert result.completed is False
    assert result.diagnostics == []


def test_clang_adapter_parses_path_sensitive_plist(monkeypatch):
    monkeypatch.setattr("app.services.code_index.clang_static_analyzer.shutil.which", lambda _name: "/usr/bin/clang")
    monkeypatch.setattr("app.services.code_index.clang_static_analyzer._checker_argument", lambda _path: "core,unix.Malloc")

    def fake_run(command, **_kwargs):
        source_path = Path(command[command.index("-o") - 1])
        output_path = Path(command[command.index("-o") + 1])
        payload = {
            "files": [str(source_path)],
            "diagnostics": [
                {
                    "check_name": "unix.Malloc",
                    "category": "Memory error",
                    "description": "Potential leak of memory pointed to by 'buffer'",
                    "location": {"line": 4, "col": 5, "file": 0},
                    "path": [
                        {"location": {"line": 2, "col": 20, "file": 0}, "message": "Memory is allocated"},
                        {"location": {"line": 4, "col": 5, "file": 0}, "message": "Memory is never released"},
                    ],
                }
            ],
        }
        output_path.write_bytes(plistlib.dumps(payload))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.services.code_index.clang_static_analyzer.subprocess.run", fake_run)
    source = ReviewFile(
        relative_path="src/leak.c",
        source_text="""#include <stdlib.h>
int leak(void) {
    char *buffer = malloc(16);
    return buffer != 0;
}
""",
        size_bytes=100,
    )

    result = run_clang_static_analysis([source], _settings())
    findings = diagnostics_to_findings(result.diagnostics)

    assert result.available is True
    assert result.completed is True
    assert result.analyzed_files == {"src/leak.c"}
    assert len(result.diagnostics[0].path) == 2
    assert findings[0].category.value == "resource_leak"
    assert findings[0].severity.value == "high"
    assert findings[0].file_path == "src/leak.c"
    assert findings[0].line == 4


def test_clang_adapter_preserves_clean_analysis(monkeypatch):
    monkeypatch.setattr("app.services.code_index.clang_static_analyzer.shutil.which", lambda _name: "/usr/bin/clang")
    monkeypatch.setattr("app.services.code_index.clang_static_analyzer._checker_argument", lambda _path: "core,unix.Malloc")

    def fake_run(command, **_kwargs):
        Path(command[command.index("-o") + 1]).write_bytes(plistlib.dumps({"files": [], "diagnostics": []}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.services.code_index.clang_static_analyzer.subprocess.run", fake_run)
    source = ReviewFile(
        relative_path="clean.c",
        source_text="int main(void) { return 0; }",
        size_bytes=28,
    )

    result = run_clang_static_analysis([source], _settings())

    assert result.completed is True
    assert result.analyzed_files == {"clean.c"}
    assert result.diagnostics == []


@pytest.mark.skipif(
    shutil.which("clang") is None or shutil.which("CodeChecker") is None,
    reason="real Clang CTU acceptance requires clang and CodeChecker",
)
def test_real_clang_ctu_resource_lifecycle_gold_set():
    root = Path(__file__).parent / "fixtures" / "static_analysis" / "resource_lifecycle"
    files = []
    for path in sorted(root.glob("*.fixture")):
        source_text = path.read_text(encoding="utf-8")
        relative_path = path.name.removesuffix(".fixture")
        files.append(
            ReviewFile(
                relative_path=relative_path,
                source_text=source_text,
                size_bytes=len(source_text.encode("utf-8")),
            )
        )

    result = run_clang_static_analysis(
        files,
        Settings(
            _env_file=None,
            allow_insecure_defaults=True,
            clang_static_analysis_enabled=True,
            clang_static_analysis_ctu_enabled=True,
        ),
    )

    assert result.completed is True
    assert result.errors == []
    assert {(item.file_path, item.line) for item in result.diagnostics} == {
        ("review_cases.c", 34),
        ("review_cases.c", 60),
        ("review_cases.c", 78),
    }
