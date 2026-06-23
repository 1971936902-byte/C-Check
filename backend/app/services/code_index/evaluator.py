from __future__ import annotations

from dataclasses import dataclass, field
from math import log2
from time import perf_counter
from typing import Callable, TypeVar

from app.services.code_index.retriever import RetrievedContext


@dataclass(frozen=True)
class RagEvaluationResult:
    recall_at_k: float
    precision_at_k: float
    mrr: float
    token_waste_ratio: float
    negative_hit_rate: float = 0.0
    ndcg_at_k: float = 0.0


@dataclass(frozen=True)
class GoldRetrievalCase:
    name: str
    retrieved: list[RetrievedContext]
    must_retrieve: set[str]
    must_not_retrieve: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class GoldEvaluationSummary:
    case_count: int
    recall_at_k: float
    precision_at_k: float
    mrr: float
    ndcg_at_k: float
    token_waste_ratio: float
    negative_hit_rate: float
    failed_cases: list[str]


@dataclass(frozen=True)
class EvidenceQualityResult:
    evidence_coverage: float
    citation_accuracy: float


@dataclass(frozen=True)
class GraphQualityResult:
    call_edge_accuracy: float
    declaration_definition_match_rate: float


@dataclass(frozen=True)
class FindingQualityResult:
    finding_precision: float
    finding_recall: float
    f1: float


@dataclass(frozen=True)
class LatencyResult:
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float


T = TypeVar("T")


def evaluate_retrieval(
    retrieved: list[RetrievedContext],
    must_retrieve: set[str],
    *,
    k: int = 10,
    must_not_retrieve: set[str] | None = None,
) -> RagEvaluationResult:
    top = retrieved[:k]
    must_not_retrieve = must_not_retrieve or set()
    if not must_retrieve:
        negative_hits = sum(1 for context in top if _matches_any(context, must_not_retrieve))
        return RagEvaluationResult(
            recall_at_k=1.0,
            precision_at_k=1.0,
            mrr=1.0,
            token_waste_ratio=0.0,
            negative_hit_rate=negative_hits / len(top) if top else 0.0,
            ndcg_at_k=1.0,
        )
    matched_positions = [
        index
        for index, context in enumerate(top, start=1)
        if _matches_any(context, must_retrieve)
    ]
    negative_hits = sum(1 for context in top if _matches_any(context, must_not_retrieve))
    recall = min(1.0, len({_matched_key(context, must_retrieve) for context in top if _matches_any(context, must_retrieve)}) / len(must_retrieve))
    precision = len(matched_positions) / len(top) if top else 0.0
    mrr = 1 / matched_positions[0] if matched_positions else 0.0
    token_waste = 1.0 - precision if top else 1.0
    ndcg = _ndcg(top, must_retrieve)
    return RagEvaluationResult(
        recall_at_k=recall,
        precision_at_k=precision,
        mrr=mrr,
        token_waste_ratio=token_waste,
        negative_hit_rate=negative_hits / len(top) if top else 0.0,
        ndcg_at_k=ndcg,
    )


def evaluate_gold_cases(cases: list[GoldRetrievalCase], *, k: int = 10) -> GoldEvaluationSummary:
    if not cases:
        return GoldEvaluationSummary(
            case_count=0,
            recall_at_k=0.0,
            precision_at_k=0.0,
            mrr=0.0,
            ndcg_at_k=0.0,
            token_waste_ratio=1.0,
            negative_hit_rate=0.0,
            failed_cases=[],
        )
    results = [
        evaluate_retrieval(
            case.retrieved,
            case.must_retrieve,
            k=k,
            must_not_retrieve=case.must_not_retrieve,
        )
        for case in cases
    ]
    failed_cases = [
        case.name
        for case, result in zip(cases, results)
        if result.recall_at_k < 1.0 or result.negative_hit_rate > 0
    ]
    count = len(results)
    return GoldEvaluationSummary(
        case_count=count,
        recall_at_k=sum(result.recall_at_k for result in results) / count,
        precision_at_k=sum(result.precision_at_k for result in results) / count,
        mrr=sum(result.mrr for result in results) / count,
        ndcg_at_k=sum(result.ndcg_at_k for result in results) / count,
        token_waste_ratio=sum(result.token_waste_ratio for result in results) / count,
        negative_hit_rate=sum(result.negative_hit_rate for result in results) / count,
        failed_cases=failed_cases,
    )


def evaluate_evidence_quality(
    required_evidence: set[str],
    selected_evidence: list[RetrievedContext],
    cited_evidence_ids: set[str],
) -> EvidenceQualityResult:
    selected_keys = {_matched_key(context, required_evidence) for context in selected_evidence if _matches_any(context, required_evidence)}
    selected_keys.discard("")
    evidence_coverage = len(selected_keys) / len(required_evidence) if required_evidence else 1.0
    valid_citations = {
        f"E{index}"
        for index, context in enumerate(selected_evidence, start=1)
        if _matches_any(context, required_evidence)
    }
    citation_accuracy = len(cited_evidence_ids & valid_citations) / len(cited_evidence_ids) if cited_evidence_ids else 1.0
    return EvidenceQualityResult(
        evidence_coverage=min(1.0, evidence_coverage),
        citation_accuracy=citation_accuracy,
    )


def evaluate_graph_quality(
    edges: list[tuple[str, str, str | None]],
    expected_call_edges: set[tuple[str, str]],
    expected_decl_def_matches: set[tuple[str, str]],
) -> GraphQualityResult:
    call_edges = {(source, target) for edge_type, source, target in edges if edge_type == "FUNCTION_CALLS_FUNCTION" and target}
    decl_edges = {(source, target) for edge_type, source, target in edges if edge_type == "SYMBOL_DECLARED_IN" and target}
    call_accuracy = _set_recall(call_edges, expected_call_edges)
    decl_match_rate = _set_recall(decl_edges, expected_decl_def_matches)
    return GraphQualityResult(
        call_edge_accuracy=call_accuracy,
        declaration_definition_match_rate=decl_match_rate,
    )


def evaluate_finding_quality(predicted: set[str], gold: set[str]) -> FindingQualityResult:
    true_positive = len(predicted & gold)
    precision = true_positive / len(predicted) if predicted else (1.0 if not gold else 0.0)
    recall = true_positive / len(gold) if gold else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return FindingQualityResult(finding_precision=precision, finding_recall=recall, f1=f1)


def measure_latency(samples: list[float]) -> LatencyResult:
    if not samples:
        return LatencyResult(samples=0, p50_ms=0.0, p95_ms=0.0, p99_ms=0.0)
    values = sorted(samples)
    return LatencyResult(
        samples=len(values),
        p50_ms=_percentile(values, 0.50),
        p95_ms=_percentile(values, 0.95),
        p99_ms=_percentile(values, 0.99),
    )


def time_call(fn: Callable[[], T]) -> tuple[T, float]:
    started = perf_counter()
    result = fn()
    elapsed_ms = (perf_counter() - started) * 1000
    return result, elapsed_ms


def _matches_any(context: RetrievedContext, expected: set[str]) -> bool:
    haystack = f"{context.file_path}:{context.symbol_name or ''}:{context.content}"
    return any(item in haystack for item in expected)


def _matched_key(context: RetrievedContext, expected: set[str]) -> str:
    haystack = f"{context.file_path}:{context.symbol_name or ''}:{context.content}"
    for item in expected:
        if item in haystack:
            return item
    return ""


def _ndcg(top: list[RetrievedContext], expected: set[str]) -> float:
    if not expected:
        return 1.0
    gains = [1.0 if _matches_any(context, expected) else 0.0 for context in top]
    dcg = sum(gain / log2(index + 2) for index, gain in enumerate(gains))
    ideal_hits = min(len(expected), len(top))
    idcg = sum(1.0 / log2(index + 2) for index in range(ideal_hits))
    return dcg / idcg if idcg else 0.0


def _set_recall(actual: set[tuple[str, str]], expected: set[tuple[str, str]]) -> float:
    if not expected:
        return 1.0
    return len(actual & expected) / len(expected)


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
