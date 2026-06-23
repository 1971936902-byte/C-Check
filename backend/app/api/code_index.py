from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.models import CodeEdge, CodeProject, CodeSymbol, ReviewEvidence, ReviewTask, User
from app.db.session import get_db
from app.services.code_index.context_builder import build_rag_context
from app.services.code_index.evaluator import evaluate_retrieval
from app.services.code_index.indexer import build_code_index
from app.services.code_index.planner import plan_review_units
from app.services.code_index.retriever import retrieve_context_for_files


router = APIRouter(prefix="/code-index", tags=["code-index"])
review_context_router = APIRouter(prefix="/reviews", tags=["code-index"])


class CodeIndexBuildResponse(BaseModel):
    project_id: str
    stats: dict


class CodeSymbolResponse(BaseModel):
    id: str
    kind: str
    name: str
    file_path: str
    start_line: int
    end_line: int
    confidence: float
    source_tool: str


class CodeEdgeResponse(BaseModel):
    id: str
    edge_type: str
    source_id: str
    target_id: str | None
    line: int | None
    confidence: float
    metadata: dict


class ContextPreviewResponse(BaseModel):
    context: str


class EvidenceResponse(BaseModel):
    evidence_key: str
    file_path: str
    symbol_name: str | None
    start_line: int
    end_line: int
    reason: str
    score: float


class ReviewUnitResponse(BaseModel):
    unit_id: str
    unit_type: str
    file_path: str
    symbol_name: str | None
    start_line: int
    end_line: int
    chunk_ids: list[str]


class RetrievalEvaluationResponse(BaseModel):
    recall_at_k: float
    precision_at_k: float
    mrr: float
    token_waste_ratio: float


def _owned_task(db: Session, task_id: str, current_user: User) -> ReviewTask:
    task = db.get(ReviewTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review task not found")
    if current_user.role != "admin" and task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="review task access denied")
    return task


@router.post("/{task_id}/build", response_model=CodeIndexBuildResponse)
def build_index(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CodeIndexBuildResponse:
    task = _owned_task(db, task_id, current_user)
    project = build_code_index(db, task, settings=get_settings())
    db.commit()
    return CodeIndexBuildResponse(project_id=project.id, stats=project.stats_json)


@router.get("/{task_id}/symbols", response_model=list[CodeSymbolResponse])
def list_symbols(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[CodeSymbolResponse]:
    task = _owned_task(db, task_id, current_user)
    project = _project_or_404(task)
    return [
        CodeSymbolResponse(
            id=symbol.id,
            kind=symbol.kind,
            name=symbol.name,
            file_path=symbol.file.relative_path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            confidence=symbol.confidence,
            source_tool=symbol.source_tool,
        )
        for symbol in project.symbols
    ]


@router.get("/{task_id}/graph", response_model=list[CodeEdgeResponse])
def list_graph(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[CodeEdgeResponse]:
    task = _owned_task(db, task_id, current_user)
    project = _project_or_404(task)
    return [
        CodeEdgeResponse(
            id=edge.id,
            edge_type=edge.edge_type,
            source_id=edge.source_id,
            target_id=edge.target_id,
            line=edge.line,
            confidence=edge.confidence,
            metadata=edge.metadata_json,
        )
        for edge in project.edges
    ]


@router.get("/{task_id}/review-units", response_model=list[ReviewUnitResponse])
def list_review_units(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ReviewUnitResponse]:
    task = _owned_task(db, task_id, current_user)
    project = _project_or_404(task)
    return [ReviewUnitResponse(**unit.__dict__) for unit in plan_review_units(project)]


@router.get("/{task_id}/evaluation", response_model=RetrievalEvaluationResponse)
def evaluate_context(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    must_retrieve: str = "",
    k: int = 10,
) -> RetrievalEvaluationResponse:
    task = _owned_task(db, task_id, current_user)
    retrieved = retrieve_context_for_files(db, task, task.files, settings=get_settings())
    expected = {item.strip() for item in must_retrieve.split(",") if item.strip()}
    result = evaluate_retrieval(retrieved, expected, k=k)
    return RetrievalEvaluationResponse(**result.__dict__)


@review_context_router.post("/{task_id}/contexts/preview", response_model=ContextPreviewResponse)
def preview_context(
    task_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ContextPreviewResponse:
    task = _owned_task(db, task_id, current_user)
    context = build_rag_context(db, task, task.files, settings=get_settings(), persist=False)
    return ContextPreviewResponse(context=context)


@review_context_router.get("/{task_id}/evidence/{finding_id}", response_model=list[EvidenceResponse])
def finding_evidence(
    task_id: str,
    finding_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[EvidenceResponse]:
    task = _owned_task(db, task_id, current_user)
    evidence_ids = _finding_evidence_ids(task, finding_id)
    evidence_items = [
        item
        for context in task.review_contexts
        for item in context.evidence_items
        if not evidence_ids or item.evidence_key in evidence_ids
    ]
    return [
        EvidenceResponse(
            evidence_key=item.evidence_key,
            file_path=item.file_path,
            symbol_name=item.symbol_name,
            start_line=item.start_line,
            end_line=item.end_line,
            reason=item.reason,
            score=item.score,
        )
        for item in evidence_items
    ]


def _project_or_404(task: ReviewTask) -> CodeProject:
    if task.code_project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="code index not found")
    return task.code_project


def _finding_evidence_ids(task: ReviewTask, finding_id: str) -> set[str]:
    if task.report is None:
        return set()
    findings = task.report.result_json.get("findings", [])
    if finding_id.isdigit():
        index = int(finding_id) - 1
        if 0 <= index < len(findings):
            return set(findings[index].get("evidence_ids") or [])
    return set()
