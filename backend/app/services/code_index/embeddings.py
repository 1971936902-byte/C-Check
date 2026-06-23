from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import CodeChunk, CodeEmbedding, CodeProject
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


def sync_project_embeddings(
    db: Session,
    project: CodeProject,
    *,
    settings: Settings | None = None,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> list[EmbeddedChunk]:
    settings = settings or get_settings()
    existing = {
        embedding.chunk_id: embedding
        for embedding in db.scalars(
            select(CodeEmbedding).where(
                CodeEmbedding.project_id == project.id,
                CodeEmbedding.embedding_model == embedding_model,
            )
        ).all()
    }
    embedded: list[EmbeddedChunk] = []
    for chunk in project.chunks:
        vector = embed_text(chunk.content)
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
) -> int:
    settings = settings or get_settings()
    client = QdrantCodeIndexClient(settings)
    if not client.enabled:
        return 0
    embedded = sync_project_embeddings(db, project, settings=settings)
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
) -> int:
    settings = settings or get_settings()
    client = QdrantCodeIndexClient(settings)
    if not client.enabled:
        return 0
    embedded = sync_project_embeddings(db, project, settings=settings)
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


def _vector_hash(vector: list[float]) -> str:
    packed = ",".join(f"{value:.6f}" for value in vector)
    return hashlib.sha256(packed.encode("ascii")).hexdigest()


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
