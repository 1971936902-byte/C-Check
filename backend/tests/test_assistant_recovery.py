from datetime import datetime, timedelta

import pytest

from app.db.models import AssistantComputeGate, AssistantDocument, ModelNode, ReviewTask, TaskStatus, User
from app.services import assistant_gate as gate, assistant_recovery as recovery


TOKEN = 'd' * 64


@pytest.fixture
def sessions(db_session_factory):
    with db_session_factory() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        db.commit()
    return db_session_factory


def test_installer_fence_survives_gateway_completion_and_failure(sessions):
    now = datetime(2026, 10, 28)
    with sessions() as db:
        owner = gate.claim(db, 'chat', now=now)
        gate.checkpoint_upstream(db, owner, 'a' * 64, 'old-engine')
        recovery.begin(db, TOKEN)
        db.commit()
    with sessions() as db:
        assert not gate.heartbeat(db, owner, now=now + timedelta(seconds=31))
        gate.fail_closed(db, owner, 'upstream lost')
        assert gate.recovery_candidate(db) is None
        db.commit()
    with sessions() as db:
        assert db.get(AssistantComputeGate, 1).fault == recovery.marker(TOKEN)
        assert gate.acknowledge_idle(db, owner)
        db.commit()
    with sessions() as db:
        assert not gate.review_may_start(db)
        with pytest.raises(gate.AssistantUnavailable):
            gate.claim(db, 'embedding')
        db.rollback()
        assert db.get(AssistantComputeGate, 1).fault == recovery.marker(TOKEN)
        with pytest.raises(RuntimeError, match='identity mismatch'):
            recovery.finish(db, 'e' * 64)
        db.rollback()
        recovery.finish(db, TOKEN)
        db.commit()
    with sessions() as db:
        assert gate.review_may_start(db)


@pytest.mark.parametrize('status,graph', [(TaskStatus.RUNNING, 'idle'), (TaskStatus.COMPLETED, 'running')])
def test_active_review_and_graph_refuse_recovery(sessions, status, graph):
    with sessions() as db:
        task = ReviewTask(owner=User(username='busy', password_hash='unused'),
            model_node=ModelNode(display_name='Qwen', model_identifier='qwen', base_url='http://unused'),
            status=status, graph_build_status=graph, input_mode='text', display_name='busy.c', file_count=1)
        db.add(task)
        db.commit()
    with sessions() as db:
        with pytest.raises(RuntimeError, match='work is active'):
            recovery.begin(db, TOKEN)
        db.rollback()
        assert db.get(AssistantComputeGate, 1).fault is None


def test_confirmed_process_replacement_clears_old_epoch_and_pauses_only_incomplete_documents(sessions):
    with sessions() as db:
        user = User(username='recover-docs', password_hash='unused')
        db.add(user)
        db.flush()
        for status in ('processing', 'recovery_required', 'ready', 'queued'):
            db.add(AssistantDocument(owner_id=user.id, filename=status, status=status, size_bytes=1, sha256='a' * 64))
        owner = gate.claim(db, 'embedding')
        gate.checkpoint_upstream(db, owner, 'a' * 64, 'dead-engine', operation='embedding')
        recovery.begin(db, TOKEN)
        db.commit()
    with sessions() as db:
        recovery.begin(db, TOKEN)
        assert recovery.finish(db, TOKEN) == 2
        db.commit()
    with sessions() as db:
        state = db.get(AssistantComputeGate, 1)
        assert state.owner_token is state.control_token is state.control_epoch is None
        assert gate.status(db)['available']
        from sqlalchemy import select
        statuses = {row.filename: row.status for row in db.scalars(select(AssistantDocument))}
        assert statuses == {'processing': 'paused', 'recovery_required': 'paused', 'ready': 'ready', 'queued': 'queued'}
        fresh = gate.claim(db, 'chat')
        assert not gate.acknowledge_idle(db, owner)
        db.commit()
        assert db.get(AssistantComputeGate, 1).owner_token == fresh


def test_another_installer_cannot_replace_pending_recovery(sessions):
    with sessions() as db:
        recovery.begin(db, TOKEN)
        db.commit()
    with sessions() as db:
        with pytest.raises(RuntimeError, match='different installer'):
            recovery.begin(db, 'e' * 64)
        db.rollback()
        assert db.get(AssistantComputeGate, 1).fault == recovery.marker(TOKEN)
