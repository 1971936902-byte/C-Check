"""Privileged installer recovery after owned model processes have been replaced."""
import re

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.db.models import AssistantDocument, ReviewTask, TaskStatus
from app.services import assistant_gate as gate


def marker(token):
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{64}', token):
        raise ValueError('invalid installer recovery identity')
    return gate.RECOVERY_FENCE_PREFIX + token


def begin(db, token):
    state = gate.lock_gate(db)
    expected = marker(token)
    if gate.installer_recovery(state) and state.fault != expected:
        raise RuntimeError('a different installer recovery is pending')
    active = select(ReviewTask.id).where(
        (ReviewTask.status == TaskStatus.RUNNING) | (ReviewTask.graph_build_status == 'running')
    ).limit(1).with_for_update(nowait=True)
    try:
        busy = db.scalar(active)
    except OperationalError as error:
        raise RuntimeError('review or graph work is active; retry after completion') from error
    if busy:
        raise RuntimeError('review or graph work is active; retry after completion')
    state.fault = expected
    state.cancel_requested = bool(state.owner_token)
    db.flush()


def finish(db, token):
    # Only the trusted installer calls this after stopping assistant writers,
    # replacing both owned inference containers, and verifying their health.
    state = gate.lock_gate(db)
    if state.fault != marker(token):
        raise RuntimeError('installer recovery identity mismatch')
    state.owner_token = None
    state.control_token = None
    state.control_epoch = None
    state.operation = None
    state.heartbeat_at = None
    state.cancel_requested = False
    state.fault = None
    result = db.execute(update(AssistantDocument).where(
        AssistantDocument.status.in_(['processing', 'recovery_required'])
    ).values(status='paused', error='interrupted_processing; retry_available'))
    db.flush()
    return result.rowcount
