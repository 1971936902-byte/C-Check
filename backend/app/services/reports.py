from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.db.models import Report, ReviewTask
from app.schemas.model_response import ModelReviewResponse


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


def build_report(task: ReviewTask, result: ModelReviewResponse) -> Report:
    return populate_report(Report(task=task), task, result)


def render_markdown(report: Report) -> str:
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
    findings = report.result_json.get("findings", [])
    if not findings:
        lines.append("No findings.")
    for index, finding in enumerate(findings, start=1):
        line = finding.get("line")
        location = finding.get("file_path", "unknown")
        if line:
            location = f"{location}:{line}"
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
