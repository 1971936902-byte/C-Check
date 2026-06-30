from app.core.config import Settings
from app.services.code_index.qdrant import (
    QDRANT_UPSERT_BATCH_SIZE,
    QdrantCodeIndexClient,
    QdrantPoint,
    _point_batches,
    _qdrant_payload,
    _qdrant_point_id,
)


def test_qdrant_point_id_converts_internal_ids_to_stable_uuid():
    internal_id = "project:chunk:hashing-code-embedding-v1"

    first = _qdrant_point_id(internal_id)
    second = _qdrant_point_id(internal_id)

    assert first == second
    assert len(first) == 36
    assert _qdrant_point_id(first) == first


def test_qdrant_payload_keeps_original_vector_id_for_traceability():
    point = QdrantPoint(
        point_id="project:chunk:hashing-code-embedding-v1",
        vector=[1.0, 0.0],
        payload={"project_id": "project", "chunk_id": "chunk"},
    )

    payload = _qdrant_payload(point)

    assert payload["project_id"] == "project"
    assert payload["chunk_id"] == "chunk"
    assert payload["vector_id"] == point.point_id


def test_qdrant_client_exposes_sync_and_async_search_paths():
    client = QdrantCodeIndexClient(Settings(_env_file=None, allow_insecure_defaults=True))

    assert callable(client.search)
    assert callable(client.search_sync)


def test_qdrant_upserts_are_split_into_bounded_batches():
    points = [QdrantPoint(point_id=str(index), vector=[1.0], payload={}) for index in range(QDRANT_UPSERT_BATCH_SIZE * 2 + 1)]

    batches = _point_batches(points)

    assert [len(batch) for batch in batches] == [QDRANT_UPSERT_BATCH_SIZE, QDRANT_UPSERT_BATCH_SIZE, 1]
