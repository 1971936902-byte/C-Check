from __future__ import annotations

from dataclasses import dataclass

from app.services.code_index.retriever import RetrievedContext


@dataclass(frozen=True)
class RagEvaluationResult:
    recall_at_k: float
    precision_at_k: float
    mrr: float
    token_waste_ratio: float


def evaluate_retrieval(
    retrieved: list[RetrievedContext],
    must_retrieve: set[str],
    *,
    k: int = 10,
) -> RagEvaluationResult:
    top = retrieved[:k]
    if not must_retrieve:
        return RagEvaluationResult(recall_at_k=1.0, precision_at_k=1.0, mrr=1.0, token_waste_ratio=0.0)
    matched_positions = [
        index
        for index, context in enumerate(top, start=1)
        if _matches_any(context, must_retrieve)
    ]
    recall = min(1.0, len({_matched_key(context, must_retrieve) for context in top if _matches_any(context, must_retrieve)}) / len(must_retrieve))
    precision = len(matched_positions) / len(top) if top else 0.0
    mrr = 1 / matched_positions[0] if matched_positions else 0.0
    token_waste = 1.0 - precision if top else 1.0
    return RagEvaluationResult(recall_at_k=recall, precision_at_k=precision, mrr=mrr, token_waste_ratio=token_waste)


def _matches_any(context: RetrievedContext, expected: set[str]) -> bool:
    haystack = f"{context.file_path}:{context.symbol_name or ''}:{context.content}"
    return any(item in haystack for item in expected)


def _matched_key(context: RetrievedContext, expected: set[str]) -> str:
    haystack = f"{context.file_path}:{context.symbol_name or ''}:{context.content}"
    for item in expected:
        if item in haystack:
            return item
    return ""
