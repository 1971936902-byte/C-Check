from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, CodeEmbedding, CodeEmbeddingCache, CodeProject
from app.services.code_index.qdrant import QdrantCodeIndexClient, QdrantPoint


DEFAULT_EMBEDDING_MODEL = "hashing-code-embedding-v1"
DEFAULT_DIMENSION = 128


@dataclass(frozen=True)
class EmbeddedChunk:
    chunk: CodeChunk
    vector_id: str
    vector: list[float]


def embed_text(text: str, *, dimension: int = DEFAULT_DIMENSION) -> list[float]:
    vector = [0.0] * dimension
    tokens = _tokens(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8", errors="ignore")).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embed_text_with_settings(text: str, settings: Settings | None = None) -> list[float]:
    return embed_texts_with_settings([text], settings, input_type="query")[0]


def embed_texts_with_settings(
    texts: list[str],
    settings: Settings | None = None,
    *,
    input_type: str = "passage",
) -> list[list[float]]:
    settings = settings or get_settings()
    if not texts:
        return []
    if settings.rag_embedding_backend.lower() in {"openai", "openai-compatible", "http"} and settings.rag_embedding_base_url:
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), settings.rag_embedding_batch_size):
                vectors.extend(_embed_texts_remote(texts[start : start + settings.rag_embedding_batch_size], settings, input_type=input_type))
            return vectors
        except Exception:
            if not settings.rag_embedding_allow_hash_fallback:
                raise
    return [embed_text(text, dimension=settings.rag_embedding_dimension) for text in texts]


def sync_project_embeddings(
    db: Session,
    project: CodeProject,
    *,
    settings: Settings | None = None,
    embedding_model: str | None = None,
    cache_stats: dict[str, int] | None = None,
) -> list[EmbeddedChunk]:
    settings = settings or get_settings()
    embedding_model = embedding_model or settings.rag_embedding_model or DEFAULT_EMBEDDING_MODEL
    existing = {
        embedding.chunk_id: embedding
        for embedding in db.scalars(
            select(CodeEmbedding).where(
                CodeEmbedding.project_id == project.id,
                CodeEmbedding.embedding_model == embedding_model,
            )
        ).all()
    }
    chunks = list(project.chunks)
    signature = _embedding_signature(settings, embedding_model)
    vectors_by_hash: dict[str, list[float]] = {}
    cached_rows: dict[str, CodeEmbeddingCache] = {}
    if settings.rag_cache_enabled and chunks:
        cached_rows = {
            row.content_hash: row
            for row in db.scalars(
                select(CodeEmbeddingCache).where(
                    CodeEmbeddingCache.embedding_signature == signature,
                    CodeEmbeddingCache.content_hash.in_({chunk.content_hash for chunk in chunks}),
                )
            ).all()
            if row.dimension == settings.rag_embedding_dimension
        }
        for content_hash, row in cached_rows.items():
            vectors_by_hash[content_hash] = [float(value) for value in row.vector_json]

    missing_by_hash: dict[str, CodeChunk] = {}
    for chunk in chunks:
        if chunk.content_hash not in vectors_by_hash:
            missing_by_hash.setdefault(chunk.content_hash, chunk)
    missing_chunks = list(missing_by_hash.values())
    missing_vectors = embed_texts_with_settings(
        [chunk.content for chunk in missing_chunks], settings, input_type="passage"
    )
    for chunk, vector in zip(missing_chunks, missing_vectors, strict=True):
        vectors_by_hash[chunk.content_hash] = vector
        if settings.rag_cache_enabled:
            db.add(
                CodeEmbeddingCache(
                    content_hash=chunk.content_hash,
                    embedding_signature=signature,
                    dimension=len(vector),
                    vector_json=vector,
                    hit_count=0,
                )
            )
    for chunk in chunks:
        if chunk.content_hash in cached_rows:
            cached_rows[chunk.content_hash].hit_count += 1
    if cache_stats is not None:
        cache_stats["hits"] = sum(1 for chunk in chunks if chunk.content_hash in cached_rows)
        cache_stats["misses"] = len(missing_chunks)

    embedded: list[EmbeddedChunk] = []
    for chunk in chunks:
        vector = vectors_by_hash[chunk.content_hash]
        vector_hash = _vector_hash(vector)
        vector_id = f"{project.id}:{chunk.id}:{embedding_model}"
        current = existing.get(chunk.id)
        if current is None:
            db.add(
                CodeEmbedding(
                    project=project,
                    chunk=chunk,
                    embedding_model=embedding_model,
                    vector_id=vector_id,
                    vector_hash=vector_hash,
                    dimension=len(vector),
                    metadata_json=_payload_for_chunk(chunk),
                )
            )
        elif current.vector_hash != vector_hash:
            current.vector_hash = vector_hash
            current.dimension = len(vector)
            current.metadata_json = _payload_for_chunk(chunk)
        embedded.append(EmbeddedChunk(chunk=chunk, vector_id=vector_id, vector=vector))
    db.flush()
    return embedded


async def upsert_project_embeddings_to_qdrant(
    db: Session,
    project: CodeProject,
    *,
    settings: Settings | None = None,
    cache_stats: dict[str, int] | None = None,
) -> int:
    settings = settings or get_settings()
    client = QdrantCodeIndexClient(settings)
    if not client.enabled:
        return 0
    embedded = sync_project_embeddings(db, project, settings=settings, cache_stats=cache_stats)
    await client.upsert_points(
        [
            QdrantPoint(
                point_id=item.vector_id,
                vector=item.vector,
                payload=_payload_for_chunk(item.chunk),
            )
            for item in embedded
        ]
    )
    return len(embedded)


def upsert_project_embeddings_to_qdrant_sync(
    db: Session,
    project: CodeProject,
    *,
    settings: Settings | None = None,
    cache_stats: dict[str, int] | None = None,
) -> int:
    settings = settings or get_settings()
    client = QdrantCodeIndexClient(settings)
    if not client.enabled:
        return 0
    embedded = sync_project_embeddings(db, project, settings=settings, cache_stats=cache_stats)
    client.upsert_points_sync(
        [
            QdrantPoint(
                point_id=item.vector_id,
                vector=item.vector,
                payload=_payload_for_chunk(item.chunk),
            )
            for item in embedded
        ]
    )
    return len(embedded)


def _tokens(text: str) -> list[str]:
    import re

    return [token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", text)]


def _embed_texts_remote(texts: list[str], settings: Settings, *, input_type: str) -> list[list[float]]:
    headers = {"Content-Type": "application/json"}
    if settings.rag_embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.rag_embedding_api_key}"
    raw_prefix = settings.rag_embedding_query_prefix if input_type == "query" else settings.rag_embedding_passage_prefix
    prefix = raw_prefix.replace("\\n", "\n")
    payload = {
        "model": settings.rag_embedding_model,
        "input": [f"{prefix}{text}"[: settings.rag_embedding_max_chars] for text in texts],
    }
    with httpx.Client(timeout=settings.rag_embedding_timeout_seconds) as client:
        response = client.post(
            f"{settings.rag_embedding_base_url.rstrip('/')}/v1/embeddings",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise ValueError("embedding response row count does not match input")
    vectors: list[list[float]] = []
    for row in sorted(rows, key=lambda item: int(item.get("index", 0))):
        vector = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(vector, list) or not vector:
            raise ValueError("embedding response missing data[].embedding")
        values = [float(item) for item in vector]
        if len(values) != settings.rag_embedding_dimension:
            raise ValueError(
                f"embedding dimension mismatch: expected {settings.rag_embedding_dimension}, got {len(values)}"
            )
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        vectors.append([value / norm for value in values])
    return vectors


def _vector_hash(vector: list[float]) -> str:
    packed = ",".join(f"{value:.6f}" for value in vector)
    return hashlib.sha256(packed.encode("ascii")).hexdigest()


def _embedding_signature(settings: Settings, embedding_model: str) -> str:
    payload = "|".join(
        (
            settings.rag_embedding_backend,
            settings.rag_embedding_base_url or "",
            embedding_model,
            str(settings.rag_embedding_dimension),
            settings.rag_embedding_passage_prefix,
            str(settings.rag_embedding_max_chars),
        )
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _payload_for_chunk(chunk: CodeChunk) -> dict:
    return {
        "project_id": chunk.project_id,
        "chunk_id": chunk.id,
        "file_path": chunk.file.relative_path,
        "symbol_kind": chunk.chunk_kind,
        "symbol_name": chunk.symbol_name,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        **(chunk.metadata_json or {}),
    }
