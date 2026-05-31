from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.db.models import Report, ReviewTask
from app.schemas.model_response import ModelReviewResponse


class ReportRenderError(RuntimeError):
    """Raised when a report cannot be rendered in the requested format."""


def build_report(task: ReviewTask, result: ModelReviewResponse) -> Report:
    severity_counts = Counter(finding.severity.value for finding in result.findings)
    category_counts = Counter(finding.category.value for finding in result.findings)
    return Report(
        task=task,
        summary=result.summary,
        score=result.score,
        high_count=severity_counts["high"],
        medium_count=severity_counts["medium"],
        low_count=severity_counts["low"],
        suggestion_count=severity_counts["suggestion"],
        category_counts=dict(category_counts),
        result_json=result.model_dump(mode="json"),
    )


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
                f"**Remediation:** {finding['remediation']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _pdf_font(text: str) -> str:
    if text.isascii():
        return "Helvetica"
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
