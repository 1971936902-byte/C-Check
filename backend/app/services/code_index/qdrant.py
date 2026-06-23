from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings


@dataclass(frozen=True)
class QdrantPoint:
    point_id: str
    vector: list[float]
    payload: dict[str, Any]


class QdrantCodeIndexClient:
    """Small optional adapter kept out of the hot path until embeddings are enabled."""

    def __init__(self, settings: Settings) -> None:
        self.url = (settings.rag_qdrant_url or "").rstrip("/")
        self.api_key = settings.rag_qdrant_api_key
        self.collection = settings.rag_qdrant_collection

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def upsert_points(self, points: list[QdrantPoint]) -> None:
        if not self.enabled or not points:
            return
        headers = {"api-key": self.api_key} if self.api_key else {}
        payload = {
            "points": [
                {"id": point.point_id, "vector": point.vector, "payload": point.payload}
                for point in points
            ]
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(
                f"{self.url}/collections/{self.collection}/points",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
