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

    def upsert_points_sync(self, points: list[QdrantPoint]) -> None:
        if not self.enabled or not points:
            return
        headers = {"api-key": self.api_key} if self.api_key else {}
        payload = {
            "points": [
                {"id": point.point_id, "vector": point.vector, "payload": point.payload}
                for point in points
            ]
        }
        with httpx.Client(timeout=30) as client:
            response = client.put(
                f"{self.url}/collections/{self.collection}/points",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

    async def search(self, vector: list[float], *, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        headers = {"api-key": self.api_key} if self.api_key else {}
        payload = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "filter": {
                "must": [
                    {"key": "project_id", "match": {"value": project_id}},
                ]
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.url}/collections/{self.collection}/points/search",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        result = data.get("result", [])
        return result if isinstance(result, list) else []
