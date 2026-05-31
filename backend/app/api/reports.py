from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import Report, User
from app.db.session import get_db
from app.services.reports import ReportRenderError, render_markdown, render_pdf


router = APIRouter(prefix="/reports", tags=["reports"])


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    summary: str
    score: float
    high_count: int
    medium_count: int
    low_count: int
    suggestion_count: int
    category_counts: dict[str, int]
    result_json: dict


def _owned_report(db: Session, report_id: str, current_user: User) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    if report.task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="report access denied")
    return report


@router.get("/{report_id}", response_model=ReportResponse)
def get_report(
    report_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Report:
    return _owned_report(db, report_id, current_user)


@router.get("/{report_id}/markdown")
def download_markdown(
    report_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    report = _owned_report(db, report_id, current_user)
    return Response(
        render_markdown(report),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{report.id}.md"'},
    )


@router.get("/{report_id}/pdf")
def download_pdf(
    report_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    report = _owned_report(db, report_id, current_user)
    try:
        content = render_pdf(report)
    except ReportRenderError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{report.id}.pdf"'},
    )
