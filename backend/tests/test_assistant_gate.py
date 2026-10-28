from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from app.db.models import AssistantComputeGate, ModelNode, ReviewTask, TaskStatus, User
from app.services import assistant_gate as gate


@pytest.fixture
def initialized(db_session_factory):
    with db_session_factory() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        db.commit()
    return db_session_factory


def task():
    return ReviewTask(owner=User(username='reviewer', password_hash='unused'),
        model_node=ModelNode(display_name='Qwen', model_identifier='qwen2.5', base_url='http://unused'),
        input_mode='text', display_name='main.c', status=TaskStatus.QUEUED, file_count=1)


def test_control_checkpoint_is_durable_private_and_owner_fenced(initialized):
    with initialized() as db:
        token = gate.claim(db, 'chat')
        gate.checkpoint_upstream(db, token, 'a' * 64)
        db.commit()
    with initialized() as db:
        state = db.get(AssistantComputeGate, 1)
        assert state.control_token == 'a' * 64 and state.control_epoch is None
        gate.checkpoint_upstream(db, token, 'a' * 64, 'engine-one')
        gate.fail_closed(db, token, 'lost connection')
        db.commit()
    with initialized() as db:
        assert db.get(AssistantComputeGate, 1).control_epoch == 'engine-one'
        assert 'control_token' not in gate.status(db)
        assert 'control_epoch' not in gate.status(db)
        with pytest.raises(gate.AssistantUnavailable):
            gate.checkpoint_upstream(db, 'wrong-owner', 'a' * 64, 'engine-one')
        db.rollback()
        with pytest.raises(gate.AssistantUnavailable):
            gate.checkpoint_upstream(db, token, 'b' * 64, 'engine-one')
        db.rollback()
        assert gate.acknowledge_idle(db, token)
        db.commit()
    with initialized() as db:
        state = db.get(AssistantComputeGate, 1)
        assert state.control_token is None and state.control_epoch is None


@pytest.mark.parametrize('operation,checkpoint', [('chat', False), ('chat', True), ('embedding', True)])
def test_unsubmitted_recovery_fences_late_gateway_before_dispatch(initialized, operation, checkpoint):
    now = datetime(2026, 10, 28)
    with initialized() as db:
        token = gate.claim(db, operation, now=now)
        if checkpoint:
            gate.checkpoint_upstream(db, token, 'a' * 64, operation=operation)
        db.commit()
    with initialized() as db:
        assert gate.recover_unsubmitted(db, operation=operation, now=now + timedelta(seconds=31))
        assert gate.review_may_start(db)
        db.commit()
    with initialized() as db:
        with pytest.raises(gate.AssistantUnavailable, match='assistant_ownership_lost'):
            gate.checkpoint_upstream(db, token, 'a' * 64, 'old-engine', operation=operation)
        db.rollback()
        assert gate.status(db)['available']


@pytest.mark.parametrize('operation,epoch', [('chat', None), ('chat', 'engine'), ('embedding', 'engine')])
def test_recovery_does_not_revoke_live_or_dispatched_work(initialized, operation, epoch):
    now = datetime(2026, 10, 28)
    with initialized() as db:
        token = gate.claim(db, operation, now=now)
        gate.checkpoint_upstream(db, token, 'a' * 64, epoch, operation=operation)
        if epoch:
            gate.fail_closed(db, token, 'network error')
        db.commit()
    with initialized() as db:
        assert not gate.recover_unsubmitted(db, operation=operation, now=now)
        assert db.get(AssistantComputeGate, 1).owner_token == token


def test_uncontrolled_embedding_is_not_assumed_idle(initialized):
    now = datetime(2026, 10, 28)
    with initialized() as db:
        token = gate.claim(db, 'embedding', now=now)
        db.commit()
    with initialized() as db:
        assert not gate.recover_unsubmitted(db, operation='embedding', now=now + timedelta(seconds=31))
        assert db.get(AssistantComputeGate, 1).owner_token == token


def test_only_one_assistant_claim_across_connections(initialized):
    start = Barrier(2)
    def acquire(kind):
        start.wait()
        with initialized() as db:
            try:
                token = gate.claim(db, kind)
                db.commit()
                return token
            except gate.AssistantUnavailable as exc:
                db.rollback()
                return exc.reason
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, ['chat', 'embedding']))
    assert results.count('assistant_busy') == 1
    assert len(next(v for v in results if v != 'assistant_busy')) == 64


def test_review_commit_revokes_chat_but_waits_for_upstream_idle(initialized, monkeypatch):
    monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.services.review_queue import dispatch_next_review
    sent=[]
    monkeypatch.setattr('app.services.review_queue._send_to_worker',sent.append)
    with initialized() as db:
        token=gate.claim(db,'chat')
        db.commit()
    with initialized() as db:
        gate.request_preemption(db)
        review=task()
        db.add(review)
        db.commit()
        review_id=review.id
        assert dispatch_next_review(db) is None
        assert db.get(ReviewTask,review_id).status == TaskStatus.QUEUED
    assert sent == []
    with initialized() as db:
        assert gate.heartbeat(db,token) is False
        db.commit()
        assert gate.status(db)['reason']=='review_busy'
        db.rollback()
    with initialized() as db:
        # Browser closure/cancellation request did not release the owner.
        assert db.get(AssistantComputeGate,1).owner_token==token
        assert gate.acknowledge_idle(db,token)
        db.commit()
        dispatch_next_review(db)
        assert db.get(ReviewTask,review_id).status==TaskStatus.RUNNING
    assert sent==[review_id]
    with initialized() as db:
        with pytest.raises(gate.AssistantUnavailable,match='review_busy'):
            gate.claim(db,'embedding')
        db.rollback()
        db.get(ReviewTask,review_id).status=TaskStatus.COMPLETED
        db.commit()
        gate.claim(db,'chat')
        db.commit()


def test_expiry_never_releases_gpu_ownership(initialized):
    now=datetime(2026,1,1)
    with initialized() as db:
        token=gate.claim(db,'embedding',now=now)
        db.commit()
    with initialized() as db:
        assert gate.status(db,now=now+timedelta(seconds=31))['reason']=='scheduler_recovery_required'
        assert not gate.heartbeat(db,token,now=now+timedelta(seconds=31))
        db.commit()
        assert not gate.review_may_start(db)
        db.commit()
        assert db.get(AssistantComputeGate,1).owner_token==token
        with pytest.raises(gate.AssistantUnavailable,match='recovery_required'):
            gate.claim(db,'chat',now=now+timedelta(days=1))
        db.rollback()
        assert gate.acknowledge_idle(db,token)
        db.commit()
        assert gate.status(db)['available']


def test_stale_completion_cannot_release_new_owner(initialized):
    with initialized() as db:
        old=gate.claim(db,'chat')
        db.commit()
        assert gate.acknowledge_idle(db,old)
        db.commit()
        new=gate.claim(db,'embedding')
        db.commit()
        assert not gate.acknowledge_idle(db,old)
        assert not gate.heartbeat(db,old)
        db.commit()
        assert db.get(AssistantComputeGate,1).owner_token==new


def test_failed_review_transaction_does_not_revoke_chat(initialized):
    with initialized() as db:
        token=gate.claim(db,'chat')
        db.commit()
    with initialized() as db:
        gate.request_preemption(db)
        db.add(task())
        db.flush()
        db.rollback()
    with initialized() as db:
        assert gate.heartbeat(db,token)
        assert db.scalar(select(ReviewTask.id)) is None
        db.commit()


def test_missing_scheduler_fails_closed(db_session):
    assert not gate.status(db_session)['available']
    with pytest.raises(gate.AssistantUnavailable,match='not_initialized'):
        gate.claim(db_session,'chat')


def test_graph_embedding_also_blocks_assistant(initialized):
    with initialized() as db:
        review=task()
        review.status=TaskStatus.COMPLETED
        review.graph_build_status='running'
        db.add(review)
        db.commit()
        with pytest.raises(gate.AssistantUnavailable,match='review_busy'):
            gate.claim(db,'chat')


def test_fault_blocks_review_until_idle_acknowledgement(initialized):
    with initialized() as db:
        token=gate.claim(db,'chat')
        db.commit()
        gate.fail_closed(db,token,'upstream cancellation unconfirmed')
        db.commit()
        assert not gate.review_may_start(db)
        db.commit()
        assert gate.acknowledge_idle(db,token)
        db.commit()
        assert gate.review_may_start(db)


@pytest.mark.parametrize('worker_kind', ['review', 'graph'])
def test_worker_delivery_waits_without_failing_task(initialized, monkeypatch, worker_kind):
    monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
    from app.core.config import get_settings
    get_settings.cache_clear()
    with initialized() as db:
        token = gate.claim(db, 'embedding')
        db.commit()
        review = task()
        if worker_kind == 'graph':
            review.status = TaskStatus.COMPLETED
            review.graph_build_status = 'queued'
            review.graph_build_id = 'build-1'
        db.add(review)
        db.commit()
        review_id = review.id
    with pytest.raises(gate.AssistantDrainPending):
        if worker_kind == 'review':
            from app.tasks.reviews import run_review_task
            run_review_task(review_id)
        else:
            from app.tasks.code_index import run_graph_build
            run_graph_build(review_id, 'build-1')
    with initialized() as db:
        review = db.get(ReviewTask, review_id)
        assert review.error_message is None
        if worker_kind == 'review':
            assert review.status == TaskStatus.QUEUED
        else:
            assert review.graph_build_status == 'queued'
            assert review.graph_build_error is None
        owner = db.get(AssistantComputeGate, 1)
        assert owner.owner_token == token
        assert owner.cancel_requested


def test_worker_celery_retry_policy_is_unbounded_until_drain():
    from app.tasks.reviews import dispatch_review
    from app.tasks.code_index import dispatch_graph_build
    for worker in (dispatch_review, dispatch_graph_build):
        assert gate.AssistantDrainPending in worker.autoretry_for
        assert worker.max_retries is None
        assert worker.default_retry_delay == 2
