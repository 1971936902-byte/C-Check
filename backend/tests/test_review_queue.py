from __future__ import annotations

from app.core.security import hash_password
from app.db.models import ModelNode, ReviewTask, TaskStatus, User
from app.services.review_queue import dispatch_next_review


def test_dispatch_next_review_allows_one_running_task_per_model_node(db_session_factory, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("app.services.review_queue._send_to_worker", lambda task_id: sent.append(task_id))

    with db_session_factory() as db:
        user = User(username="reviewer", password_hash=hash_password("reviewer-password"))
        node0 = ModelNode(display_name="GPU0", model_identifier="model", base_url="http://127.0.0.1:8001")
        node1 = ModelNode(display_name="GPU1", model_identifier="model", base_url="http://127.0.0.1:8002")
        running = ReviewTask(
            owner=user,
            model_node=node0,
            input_mode="text",
            display_name="running.c",
            file_count=1,
            check_types=["logic"],
            status=TaskStatus.RUNNING,
        )
        queued_same_node = ReviewTask(
            owner=user,
            model_node=node0,
            input_mode="text",
            display_name="same-node.c",
            file_count=1,
            check_types=["logic"],
            status=TaskStatus.QUEUED,
        )
        queued_other_node = ReviewTask(
            owner=user,
            model_node=node1,
            input_mode="text",
            display_name="other-node.c",
            file_count=1,
            check_types=["logic"],
            status=TaskStatus.QUEUED,
        )
        db.add_all([user, node0, node1, running, queued_same_node, queued_other_node])
        db.commit()
        same_node_id = queued_same_node.id
        other_node_id = queued_other_node.id

        dispatched = dispatch_next_review(db)

        assert dispatched is not None
        assert dispatched.id == other_node_id
        assert sent == [other_node_id]
        assert db.get(ReviewTask, other_node_id).status == TaskStatus.RUNNING
        assert db.get(ReviewTask, same_node_id).status == TaskStatus.QUEUED
