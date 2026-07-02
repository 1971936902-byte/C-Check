from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.models import ReviewFile
from app.services.code_index.clang_static_analyzer import (
    AnalyzerDiagnostic,
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


def test_clang_adapter_rejects_silent_partial_analysis(monkeypatch):
    monkeypatch.setattr("app.services.code_index.clang_static_analyzer.shutil.which", lambda _name: "/usr/bin/clang")
    files = [
        ReviewFile(relative_path="first.c", source_text="int first(void) { return 1; }", size_bytes=29),
        ReviewFile(relative_path="second.c", source_text="int second(void) { return 2; }", size_bytes=30),
    ]

    result = run_clang_static_analysis(files, _settings(clang_static_analysis_max_files=1))

    assert result.completed is False
    assert result.partial is True
    assert result.skipped_files == 1
    assert result.diagnostics == []
    assert "exceed" in result.errors[0]


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


def test_clang_adapter_maps_use_after_free_by_checker_message():
    diagnostic = AnalyzerDiagnostic(
        checker="unix.Malloc",
        category="Memory error",
        message="Use of memory after it is freed",
        file_path="use_after_free.c",
        line=12,
        column=5,
    )

    finding = diagnostics_to_findings([diagnostic])[0]

    assert finding.category.value == "memory_safety"
    assert finding.severity.value == "high"


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


@pytest.mark.skipif(
    shutil.which("clang") is None or shutil.which("CodeChecker") is None,
    reason="real Clang CTU acceptance requires clang and CodeChecker",
)
def test_real_clang_ctu_stress_gold_set():
    root = Path(__file__).parent / "fixtures" / "static_analysis" / "ctu_stress"
    files = []
    for path in sorted(root.glob("*.fixture")):
        source_text = path.read_text(encoding="utf-8")
        files.append(
            ReviewFile(
                relative_path=path.name.removesuffix(".fixture"),
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
            clang_static_analysis_jobs=1,
        ),
    )
    findings = diagnostics_to_findings(result.diagnostics)

    assert result.completed is True
    assert result.partial is False
    assert result.errors == []
    assert {(finding.line, finding.category.value) for finding in findings} == {
        (25, "resource_leak"),
        (48, "resource_leak"),
        (67, "resource_leak"),
        (78, "memory_safety"),
        (88, "memory_safety"),
    }


@pytest.mark.skipif(
    shutil.which("clang") is None or shutil.which("CodeChecker") is None,
    reason="real Clang CTU acceptance requires clang and CodeChecker",
)
def test_real_clang_ctu_resolves_nested_include_directories():
    root = Path(__file__).parent / "fixtures" / "static_analysis" / "nested_include"
    files = []
    for path in sorted(root.rglob("*.fixture")):
        source_text = path.read_text(encoding="utf-8")
        files.append(
            ReviewFile(
                relative_path=path.relative_to(root).as_posix().removesuffix(".fixture"),
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
            clang_static_analysis_jobs=1,
        ),
    )

    assert result.completed is True
    assert result.errors == []
    assert [(item.checker, item.file_path, item.line) for item in result.diagnostics] == [
        ("unix.Malloc", "src/app.c", 19)
    ]
