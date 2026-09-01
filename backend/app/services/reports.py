from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.db.models import Report, ReviewTask
from app.schemas.model_response import ModelReviewResponse


GROUP_ADJACENT_LINE_GAP = 8
CATEGORY_LABELS = {
    "buffer_overflow": "缓冲区边界风险",
    "pointer_safety": "指针安全风险",
    "memory_safety": "内存安全风险",
    "resource_leak": "资源泄漏风险",
    "integer_safety": "整数与类型转换风险",
    "logic": "逻辑风险",
}


class ReportRenderError(RuntimeError):
    """Raised when a report cannot be rendered in the requested format."""


def populate_report(report: Report, task: ReviewTask, result: ModelReviewResponse) -> Report:
    severity_counts = Counter(finding.severity.value for finding in result.findings)
    category_counts = Counter(finding.category.value for finding in result.findings)
    report.task = task
    report.summary = result.summary
    report.score = result.score
    report.high_count = severity_counts["high"]
    report.medium_count = severity_counts["medium"]
    report.low_count = severity_counts["low"]
    report.suggestion_count = severity_counts["suggestion"]
    report.category_counts = dict(category_counts)
    result_json = result.model_dump(mode="json")
    for finding in result_json.get("findings", []):
        snippet = _finding_source_snippet(task, finding.get("file_path", ""), finding.get("line"))
        if snippet:
            finding["code_snippet"] = snippet
    result_json["finding_groups"] = _build_finding_groups(task, result_json.get("findings", []))
    report.result_json = result_json
    return report


def ensure_report_display_groups(report: Report) -> Report:
    result_json = dict(report.result_json or {})
    groups = result_json.get("finding_groups") or []
    if not groups or any("primary_line" not in group for group in groups):
        result_json["finding_groups"] = _build_finding_groups(report.task, result_json.get("findings", []))
        report.result_json = result_json
    return report


def _finding_source_snippet(task: ReviewTask, file_path: str, line: int | None, *, radius: int = 2) -> list[dict]:
    if line is None:
        return []
    wanted = file_path.replace("\\", "/").strip().lstrip("./")
    matches = [
        source
        for source in task.files
        if source.relative_path.replace("\\", "/").strip().lstrip("./") == wanted
    ]
    if not matches:
        basename = wanted.rsplit("/", 1)[-1]
        matches = [source for source in task.files if source.relative_path.replace("\\", "/").rsplit("/", 1)[-1] == basename]
    if len(matches) != 1:
        return []
    lines = matches[0].source_text.splitlines()
    if line < 1 or line > len(lines):
        return []
    start, end = max(1, line - radius), min(len(lines), line + radius)
    return [
        {"line": number, "content": lines[number - 1], "kind": "removed" if number == line else "context"}
        for number in range(start, end + 1)
    ]


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./")


def _source_file_for_path(task: ReviewTask, file_path: str):
    wanted = _normalized_path(file_path)
    matches = [source for source in task.files if _normalized_path(source.relative_path) == wanted]
    if not matches:
        basename = wanted.rsplit("/", 1)[-1]
        matches = [source for source in task.files if _normalized_path(source.relative_path).rsplit("/", 1)[-1] == basename]
    return matches[0] if len(matches) == 1 else None


def _function_window_for_line(task: ReviewTask, file_path: str, line: int | None) -> dict[str, Any] | None:
    if line is None:
        return None
    source = _source_file_for_path(task, file_path)
    if source is None:
        return None
    try:
        from app.services.code_index.parser import parse_c_source

        parsed = parse_c_source(source.relative_path, source.source_text)
    except Exception:
        return None
    functions = [
        symbol
        for symbol in parsed.symbols
        if symbol.kind == "function" and symbol.start_line <= line <= symbol.end_line
    ]
    if not functions:
        return None
    function = min(functions, key=lambda symbol: (symbol.end_line - symbol.start_line, symbol.start_line))
    return {"name": function.name, "start_line": function.start_line, "end_line": function.end_line}


def _issue_notes_by_line(group: list[dict]) -> dict[int, list[str]]:
    notes: dict[int, list[str]] = {}
    for finding in group:
        line = finding.get("line")
        if not isinstance(line, int):
            continue
        title = str(finding.get("title") or finding.get("category") or "问题").strip()
        description = str(finding.get("description") or "").strip()
        note = f"{title}: {description}" if description else title
        notes.setdefault(line, []).append(note)
    return notes


def _snippet_for_lines(
    task: ReviewTask,
    file_path: str,
    lines_to_mark: list[int],
    *,
    issue_notes: dict[int, list[str]] | None = None,
    radius: int = 2,
) -> list[dict]:
    source = _source_file_for_path(task, file_path)
    if source is None or not lines_to_mark:
        return []
    lines = source.source_text.splitlines()
    valid_lines = sorted({line for line in lines_to_mark if 1 <= line <= len(lines)})
    if not valid_lines:
        return []
    start = max(1, min(valid_lines) - radius)
    end = min(len(lines), max(valid_lines) + radius)
    marked = set(valid_lines)
    snippet = []
    for number in range(start, end + 1):
        item = {"line": number, "content": lines[number - 1], "kind": "removed" if number in marked else "context"}
        notes = (issue_notes or {}).get(number) or []
        if notes:
            item["issue_title"] = "；".join(note.split(":", 1)[0] for note in notes)
            item["issue_description"] = "；".join(notes)
        snippet.append(item)
    return snippet


def _finding_sort_key(finding: dict) -> tuple[str, str, int, str, str]:
    line = finding.get("line")
    function = finding.get("_function") or {}
    return (
        _normalized_path(str(finding.get("file_path") or "")),
        str(function.get("name") or ""),
        int(line) if isinstance(line, int) else 10**9,
        str(finding.get("severity") or ""),
        str(finding.get("category") or ""),
    )


def _can_group_findings(current: dict, candidate: dict) -> bool:
    current_line = current.get("line")
    candidate_line = candidate.get("line")
    if not isinstance(current_line, int) or not isinstance(candidate_line, int):
        return False
    current_function = current.get("_function") or {}
    candidate_function = candidate.get("_function") or {}
    if not current_function.get("name") or not candidate_function.get("name"):
        return False
    return (
        _normalized_path(str(current.get("file_path") or "")) == _normalized_path(str(candidate.get("file_path") or ""))
        and current.get("severity") == candidate.get("severity")
        and current_function.get("name") == candidate_function.get("name")
        and current_function.get("start_line") == candidate_function.get("start_line")
        and candidate_line - current_line <= GROUP_ADJACENT_LINE_GAP
    )


def _build_group_payload(task: ReviewTask, group: list[dict], group_id: int) -> dict:
    representative = group[0]
    line_numbers = sorted({int(item["line"]) for item in group if isinstance(item.get("line"), int)})
    primary_line = representative.get("line") if isinstance(representative.get("line"), int) else (line_numbers[0] if line_numbers else None)
    function = representative.get("_function") or {}
    findings = [{key: value for key, value in item.items() if key != "_function"} for item in group]
    base_title = str(representative.get("title") or "").strip()
    category = str(representative.get("category") or "")
    title = CATEGORY_LABELS.get(category, base_title or "模型发现的问题") if base_title in {"", category} else base_title
    if len(findings) > 1:
        suffix = "相关问题" if len(line_numbers) == 1 else "相关位置"
        title = f"{title} · {len(findings)} 个{suffix}"
    descriptions = [str(item.get("description") or "").strip() for item in group if item.get("description")]
    description = descriptions[0] if len(descriptions) == 1 else "\n".join(f"- {text}" for text in descriptions)
    related_lines = [line for line in line_numbers if line != primary_line]
    issue_notes = _issue_notes_by_line(group)
    return {
        "id": f"group-{group_id}",
        "severity": representative.get("severity"),
        "category": category,
        "title": title,
        "description": description,
        "file_path": representative.get("file_path"),
        "line": primary_line,
        "primary_line": primary_line,
        "line_numbers": line_numbers,
        "related_line_numbers": related_lines,
        "function_name": function.get("name"),
        "function_start_line": function.get("start_line"),
        "function_end_line": function.get("end_line"),
        "findings": findings,
        "code_snippet": _snippet_for_lines(
            task,
            str(representative.get("file_path") or ""),
            line_numbers,
            issue_notes=issue_notes,
        ),
    }


def _build_finding_groups(task: ReviewTask, findings: list[dict]) -> list[dict]:
    annotated = []
    for finding in findings:
        line = finding.get("line")
        annotated.append(
            {
                **finding,
                "_function": _function_window_for_line(
                    task,
                    str(finding.get("file_path") or ""),
                    line if isinstance(line, int) else None,
                ),
            }
        )
    groups: list[list[dict]] = []
    for finding in sorted(annotated, key=_finding_sort_key):
        if groups and _can_group_findings(groups[-1][-1], finding):
            groups[-1].append(finding)
        else:
            groups.append([finding])
    return [_build_group_payload(task, group, index) for index, group in enumerate(groups, start=1)]


def build_report(task: ReviewTask, result: ModelReviewResponse) -> Report:
    return populate_report(Report(task=task), task, result)


def render_markdown(report: Report) -> str:
    report = ensure_report_display_groups(report)
    task = report.task
    lines = [
        "# C Language Code Review Report",
        "",
        f"- Task: `{task.id}`",
        f"- Submission: `{task.display_name}`",
        f"- Model: `{task.model_node.display_name}`",
        f"- Score: `{report.score:g}`",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Findings",
        "",
    ]
    findings = report.result_json.get("finding_groups") or report.result_json.get("findings", [])
    if not findings:
        lines.append("No findings.")
    for index, finding in enumerate(findings, start=1):
        line_numbers = finding.get("line_numbers") or ([finding.get("line")] if finding.get("line") else [])
        location = finding.get("file_path", "unknown")
        if line_numbers:
            location = f"{location}:{line_numbers[0]} 行" if len(line_numbers) == 1 else f"{location}:{line_numbers} 行"
        lines.extend(
            [
                f"### {index}. [{finding['severity'].upper()}] {finding['title']}",
                "",
                f"- Category: `{finding['category']}`",
                f"- Location: `{location}`",
                "",
                finding["description"],
                "",
            ]
        )
        grouped = finding.get("findings") or []
        if len(grouped) > 1:
            primary_line = finding.get("primary_line")
            related_lines = finding.get("related_line_numbers") or []
            if primary_line:
                lines.append(f"- 主问题行: `{primary_line}`")
            if related_lines:
                lines.append(f"- 关联行: `{related_lines}`")
            lines.append("- 相关问题:")
            for item in grouped:
                lines.append(f"  - 第 {item.get('line')} 行: {item.get('title')}")
            lines.append("")
        snippet = finding.get("code_snippet") or []
        if snippet:
            lines.extend(
                [
                    "```c",
                    *[f"{item.get('line', '')}: {item.get('content', '')}" for item in snippet],
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def render_text(report: Report) -> str:
    return render_markdown(report).replace("`", "").replace("### ", "").replace("## ", "").replace("# ", "")


def _pdf_font(text: str) -> str:
    if text.isascii():
        return "Helvetica"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:
        pass
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    for path in candidates:
        if path.exists():
            try:
                pdfmetrics.registerFont(TTFont("CCheckUnicode", str(path)))
                return "CCheckUnicode"
            except Exception:
                continue
    raise ReportRenderError("PDF export requires an installed Chinese-compatible system font")


def render_pdf(report: Report) -> bytes:
    markdown = render_markdown(report)
    font_name = _pdf_font(markdown)
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = font_name
    story = []
    for line in markdown.splitlines():
        if line.startswith("```"):
            continue
        if not line:
            story.append(Spacer(1, 8))
            continue
        if line.startswith("# "):
            style = styles["Title"]
        elif line.startswith("## "):
            style = styles["Heading2"]
        elif line.startswith("### "):
            style = styles["Heading3"]
        else:
            style = styles["BodyText"]
        story.append(Paragraph(escape(line.lstrip("# ").replace("`", "")), style))
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build(story)
    return buffer.getvalue()
