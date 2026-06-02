from __future__ import annotations

import io
import stat
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.security import create_access_token, hash_password
from app.db.models import ModelNode, ReviewFile, ReviewTask, User


JWT_SECRET = "test-jwt-secret-at-least-32-characters-long"


@pytest.fixture(autouse=True)
def use_secure_test_jwt_secret(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_zip(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.create_system = 3
                info.external_attr = mode << 16
            zipped.writestr(info, content)
    return archive.getvalue()


def add_user_and_node(db_session_factory, *, enabled: bool = True):
    with db_session_factory() as db:
        user = User(username="reviewer", password_hash=hash_password("reviewer-password"))
        other = User(username="other", password_hash=hash_password("other-password"))
        node = ModelNode(
            display_name="Review node",
            model_identifier="review-model",
            base_url="http://model-node",
            is_enabled=enabled,
        )
        db.add_all([user, other, node])
        db.commit()
        return user.id, other.id, node.id


def auth_headers(user_id: str) -> dict[str, str]:
    token = create_access_token(user_id, 0, JWT_SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def test_collect_text_rejects_empty_input():
    from app.services.submissions import SubmissionError, collect_text_submission

    with pytest.raises(SubmissionError, match="must not be empty"):
        collect_text_submission(" \r\n\t")


def test_collect_text_uses_snippet_filename():
    from app.services.submissions import collect_text_submission

    submission = collect_text_submission("int main(void) { return 0; }")

    assert submission.display_name == "snippet.c"
    assert submission.files[0].relative_path == "snippet.c"


def test_collect_text_rejects_oversized_input():
    from app.services.submissions import SubmissionError, collect_text_submission

    settings = Settings(_env_file=None, upload_max_file_bytes=4)

    with pytest.raises(SubmissionError, match="size limit"):
        collect_text_submission("12345", settings)


@pytest.mark.parametrize("filename", ["main.cpp", "README", "main.c.exe"])
def test_collect_single_file_rejects_non_c_extension(filename):
    from app.services.submissions import SubmissionError, collect_file_submission

    with pytest.raises(SubmissionError, match="extension"):
        collect_file_submission(filename, b"int main(void) {}", Settings(_env_file=None))


def test_collect_single_file_rejects_oversized_file():
    from app.services.submissions import SubmissionError, collect_file_submission

    settings = Settings(_env_file=None, upload_max_file_bytes=4)

    with pytest.raises(SubmissionError, match="size limit"):
        collect_file_submission("main.c", b"12345", settings)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/etc/passwd.c",
        "../escape.c",
        "src/../../escape.h",
        "src\\..\\escape.c",
        "C:/Windows/escape.c",
        "\\\\server\\share\\escape.c",
    ],
)
def test_collect_archive_rejects_absolute_and_traversal_paths(unsafe_path):
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([(unsafe_path, b"int value;", None)])

    with pytest.raises(SubmissionError, match="unsafe path"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_rejects_symlink():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("link.c", b"target.c", stat.S_IFLNK | 0o777)])

    with pytest.raises(SubmissionError, match="symbolic link"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_rejects_directory_symlink():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("linked-dir/", b"", stat.S_IFLNK | 0o777)])

    with pytest.raises(SubmissionError, match="symbolic link"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_ignores_non_c_auxiliary_files():
    from app.services.submissions import collect_archive_submission

    archive = make_zip([("src/main.c", b"int main(void) {}", None), ("notes.txt", b"x", None)])

    submission = collect_archive_submission("sources.zip", archive, Settings(_env_file=None))

    assert [source.relative_path for source in submission.files] == ["src/main.c"]


def test_collect_archive_rejects_per_file_size_limit():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("main.c", b"12345", None)])
    settings = Settings(_env_file=None, upload_max_file_bytes=4)

    with pytest.raises(SubmissionError, match="size limit"):
        collect_archive_submission("sources.zip", archive, settings)


def test_collect_archive_rejects_raw_archive_size_limit():
    from app.services.submissions import SubmissionError, collect_archive_submission

    settings = Settings(_env_file=None, upload_max_archive_bytes=4)

    with pytest.raises(SubmissionError, match="upload size limit"):
        collect_archive_submission("sources.zip", b"12345", settings)


def test_collect_archive_rejects_total_extracted_size_limit():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("one.c", b"1234", None), ("two.h", b"5678", None)])
    settings = Settings(_env_file=None, upload_max_extracted_bytes=7)

    with pytest.raises(SubmissionError, match="total extracted size"):
        collect_archive_submission("sources.zip", archive, settings)


def test_collect_archive_rejects_source_file_count_limit():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("one.c", b"1", None), ("two.h", b"2", None)])
    settings = Settings(_env_file=None, upload_max_files=1)

    with pytest.raises(SubmissionError, match="too many source files"):
        collect_archive_submission("sources.zip", archive, settings)


def test_collect_archive_rejects_metadata_entry_count_limit():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("empty-one/", b"", None), ("empty-two/", b"", None), ("main.c", b"1", None)])
    settings = Settings(_env_file=None, upload_max_archive_entries=2)

    with pytest.raises(SubmissionError, match="too many entries"):
        collect_archive_submission("sources.zip", archive, settings)


def test_collect_archive_rejects_path_length_limit():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("long-name.c", b"1", None)])
    settings = Settings(_env_file=None, upload_max_path_length=8)

    with pytest.raises(SubmissionError, match="path is too long"):
        collect_archive_submission("sources.zip", archive, settings)


def test_collect_archive_rejects_archive_without_c_source_files():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("empty/", b"", None)])

    with pytest.raises(SubmissionError, match="no C source files"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_ignores_auxiliary_files_then_rejects_when_no_c_files_remain():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("README.md", b"notes", None), ("config.json", b"{}", None)])

    with pytest.raises(SubmissionError, match="no C source files"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_rejects_only_empty_source_files():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("empty.c", b" \n", None), ("empty.h", b"", None)])

    with pytest.raises(SubmissionError, match="must not all be empty"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_maps_invalid_utf8_source_to_submission_error():
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("invalid.c", b"\xff", None)])

    with pytest.raises(SubmissionError, match="UTF-8"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_maps_unsupported_zip_compression_to_submission_error(monkeypatch):
    from app.services.submissions import SubmissionError, collect_archive_submission

    archive = make_zip([("main.c", b"int value;", None)])

    def raise_unsupported_compression(*_args, **_kwargs):
        raise NotImplementedError("unsupported compression")

    monkeypatch.setattr(zipfile.ZipFile, "read", raise_unsupported_compression)

    with pytest.raises(SubmissionError, match="invalid zip archive"):
        collect_archive_submission("sources.zip", archive, Settings(_env_file=None))


def test_collect_archive_returns_normalized_c_files():
    from app.services.submissions import collect_archive_submission

    archive = make_zip([("src/main.c", b"int main(void) {}", None), ("include/main.h", b"#pragma once", None)])

    submission = collect_archive_submission("sources.zip", archive, Settings(_env_file=None))

    assert submission.display_name == "sources.zip"
    assert [source.relative_path for source in submission.files] == ["src/main.c", "include/main.h"]


def test_collect_text_rejects_unencodable_surrogate_input():
    from app.services.submissions import SubmissionError, collect_text_submission

    with pytest.raises(SubmissionError, match="UTF-8"):
        collect_text_submission("\ud800")


def test_text_submission_persists_task_and_file(db_session_factory):
    from app.main import app

    user_id, _, node_id = add_user_and_node(db_session_factory)

    with TestClient(app) as client:
        response = client.post(
            "/api/reviews/text",
            headers=auth_headers(user_id),
            json={"model_node_id": node_id, "source_text": "int main(void) { return 0; }"},
        )

    assert response.status_code == 201
    with db_session_factory() as db:
        task = db.get(ReviewTask, response.json()["id"])
        files = db.scalars(select(ReviewFile).where(ReviewFile.task_id == task.id)).all()
    assert task.owner_id == user_id
    assert task.model_node_id == node_id
    assert task.input_mode == "text"
    assert task.file_count == 1
    assert files[0].relative_path == "snippet.c"


def test_file_and_archive_endpoints_accept_valid_uploads(db_session_factory):
    from app.main import app

    user_id, _, node_id = add_user_and_node(db_session_factory)
    archive = make_zip([("src/main.c", b"int main(void) {}", None)])

    with TestClient(app) as client:
        file_response = client.post(
            "/api/reviews/file",
            headers=auth_headers(user_id),
            data={"model_node_id": node_id},
            files={"file": ("main.h", b"#pragma once", "text/plain")},
        )
        archive_response = client.post(
            "/api/reviews/archive",
            headers=auth_headers(user_id),
            data={"model_node_id": node_id},
            files={"file": ("sources.zip", archive, "application/zip")},
        )

    assert file_response.status_code == 201
    assert archive_response.status_code == 201


def test_submission_rejects_missing_or_disabled_model_node(db_session_factory):
    from app.main import app

    user_id, _, disabled_node_id = add_user_and_node(db_session_factory, enabled=False)

    with TestClient(app) as client:
        missing = client.post(
            "/api/reviews/text",
            headers=auth_headers(user_id),
            json={"model_node_id": "missing-node", "source_text": "int value;"},
        )
        disabled = client.post(
            "/api/reviews/text",
            headers=auth_headers(user_id),
            json={"model_node_id": disabled_node_id, "source_text": "int value;"},
        )

    assert missing.status_code == 422
    assert disabled.status_code == 422


def test_regular_user_can_only_list_get_and_delete_owned_tasks(db_session_factory):
    from app.main import app

    user_id, other_id, node_id = add_user_and_node(db_session_factory)

    with TestClient(app) as client:
        own = client.post(
            "/api/reviews/text",
            headers=auth_headers(user_id),
            json={"model_node_id": node_id, "source_text": "int own;"},
        ).json()
        other = client.post(
            "/api/reviews/text",
            headers=auth_headers(other_id),
            json={"model_node_id": node_id, "source_text": "int other;"},
        ).json()

        listing = client.get("/api/reviews", headers=auth_headers(user_id))
        hidden_get = client.get(f"/api/reviews/{other['id']}", headers=auth_headers(user_id))
        hidden_delete = client.delete(f"/api/reviews/{other['id']}", headers=auth_headers(user_id))
        deleted = client.delete(f"/api/reviews/{own['id']}", headers=auth_headers(user_id))

    assert [task["id"] for task in listing.json()["items"]] == [own["id"]]
    assert listing.json()["total"] == 1
    assert "files" not in listing.json()["items"][0]
    assert hidden_get.status_code == 404
    assert hidden_delete.status_code == 404
    assert deleted.status_code == 204


def test_admin_reviews_endpoints_are_still_limited_to_owned_tasks(db_session_factory):
    from app.main import app

    admin_id, other_id, node_id = add_user_and_node(db_session_factory)
    with db_session_factory() as db:
        admin = db.get(User, admin_id)
        admin.role = "admin"
        db.commit()

    with TestClient(app) as client:
        other = client.post(
            "/api/reviews/text",
            headers=auth_headers(other_id),
            json={"model_node_id": node_id, "source_text": "int other;"},
        ).json()

        listing = client.get("/api/reviews", headers=auth_headers(admin_id))
        hidden_get = client.get(f"/api/reviews/{other['id']}", headers=auth_headers(admin_id))
        hidden_delete = client.delete(f"/api/reviews/{other['id']}", headers=auth_headers(admin_id))

    assert listing.json() == {"items": [], "total": 0}
    assert hidden_get.status_code == 404
    assert hidden_delete.status_code == 404


@pytest.mark.parametrize(
    ("query", "expected_status"),
    [
        ("?offset=-1", 422),
        ("?limit=0", 422),
        ("?limit=101", 422),
        ("?offset=0&limit=100", 200),
    ],
)
def test_review_list_validates_pagination_bounds(db_session_factory, query, expected_status):
    from app.main import app

    user_id, _, _ = add_user_and_node(db_session_factory)

    with TestClient(app) as client:
        response = client.get(f"/api/reviews{query}", headers=auth_headers(user_id))

    assert response.status_code == expected_status


def test_review_list_defaults_to_twenty_items_and_applies_offset(db_session_factory):
    from app.main import app

    user_id, _, node_id = add_user_and_node(db_session_factory)
    with db_session_factory() as db:
        user = db.get(User, user_id)
        node = db.get(ModelNode, node_id)
        db.add_all(
            ReviewTask(
                owner=user,
                model_node=node,
                input_mode="text",
                display_name=f"snippet-{index}.c",
            )
            for index in range(21)
        )
        db.commit()

    with TestClient(app) as client:
        first_page = client.get("/api/reviews", headers=auth_headers(user_id))
        second_page = client.get("/api/reviews?offset=20", headers=auth_headers(user_id))

    assert len(first_page.json()["items"]) == 20
    assert first_page.json()["total"] == 21
    assert len(second_page.json()["items"]) == 1
