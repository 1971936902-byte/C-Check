from sqlalchemy import func, select, text

from app.db.models import ModelNode, Report, ReviewFile, ReviewTask, User
from app.core.config import Settings
from app.api.models import ensure_mock_model_node


def test_sqlite_fixture_enables_foreign_keys(db_session):
    assert db_session.scalar(text("PRAGMA foreign_keys")) == 1


def test_deleting_user_removes_owned_review_data(db_session):
    user = User(username="reviewer", password_hash="hashed")
    node = ModelNode(
        display_name="Mock node",
        model_identifier="mock",
        base_url="http://model-node",
    )
    task = ReviewTask(
        owner=user,
        model_node=node,
        input_mode="text",
        display_name="snippet.c",
        file_count=1,
        finding_count=1,
    )
    task.files.append(ReviewFile(relative_path="snippet.c", source_text="int main(void) {}", size_bytes=17))
    task.report = Report(summary="One finding", score=80, category_counts={}, result_json={})
    db_session.add_all([user, node])
    db_session.commit()

    db_session.delete(user)
    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(ReviewTask)) == 0
    assert db_session.scalar(select(func.count()).select_from(ReviewFile)) == 0
    assert db_session.scalar(select(func.count()).select_from(Report)) == 0


def test_enabled_mock_mode_registers_mock_model_node(db_session):
    settings = Settings(_env_file=None, allow_insecure_defaults=True, mock_model_enabled=True)

    ensure_mock_model_node(db_session, settings)

    node = db_session.scalar(select(ModelNode).where(ModelNode.base_url == "mock://local"))
    assert node is not None
    assert node.display_name == "Mock 模式"
    assert node.is_enabled is True
