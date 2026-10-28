"""Database-serialized admission. Expiry never proves upstream GPU work stopped.

Mutating functions require the caller to commit or roll back. Review insertion
must request preemption in the SAME transaction as adding the queued task.
Only the gateway owning the token may acknowledge confirmed upstream quiescence.
"""
from datetime import datetime, timedelta, timezone
import secrets

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.db.models import AssistantComputeGate, ReviewTask, TaskStatus

RECOVERY_FENCE_PREFIX = 'installer recovery:'


def installer_recovery(state) -> bool:
    return bool(state.fault and state.fault.startswith(RECOVERY_FENCE_PREFIX))


class AssistantUnavailable(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AssistantDrainPending(RuntimeError):
    """Worker delivery must be retried without marking the review failed."""


def require_review_admission() -> None:
    from app.core.config import get_settings
    from app.db.session import SessionLocal

    if not get_settings().assistant_enabled:
        return
    with SessionLocal() as db:
        allowed = review_may_start(db)
        db.commit()
    if not allowed:
        raise AssistantDrainPending("waiting for confirmed assistant upstream idle")


def lock_gate(db: Session) -> AssistantComputeGate:
    # UPDATE also serializes SQLite tests; SELECT FOR UPDATE alone does not.
    result = db.execute(update(AssistantComputeGate).where(AssistantComputeGate.id == 1).values(
        generation=AssistantComputeGate.generation + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise AssistantUnavailable("scheduler_not_initialized")
    return db.scalar(select(AssistantComputeGate).where(AssistantComputeGate.id == 1)
                     .execution_options(populate_existing=True).with_for_update())


def review_pending(db: Session, *, lock=True) -> bool:
    # A locking read avoids an old MySQL REPEATABLE READ snapshot after waiting.
    query = select(ReviewTask.id).where(
        ReviewTask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING])
        | ReviewTask.graph_build_status.in_(["queued", "running"])
    ).limit(1)
    try:
        return db.scalar(query.with_for_update(nowait=True) if lock else query) is not None
    except OperationalError as error:
        # A graph worker may hold a pending review row throughout indexing.
        # Waiting here would hold the global gate and delay new review submissions.
        if (lock and db.get_bind().dialect.name == 'mysql'
                and getattr(error.orig, 'args', (None,))[0] == 3572):
            return True
        raise


def _expired(gate, now: datetime, lease_seconds: int) -> bool:
    return bool(gate.owner_token and (gate.heartbeat_at is None
                or gate.heartbeat_at <= now - timedelta(seconds=lease_seconds)))


def claim(db: Session, operation: str, *, now=None, lease_seconds=30) -> str:
    if operation not in {"chat", "embedding"}:
        raise ValueError("unknown assistant operation")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    gate = lock_gate(db)
    if gate.fault or _expired(gate, now, lease_seconds):
        raise AssistantUnavailable("scheduler_recovery_required")
    if review_pending(db):
        raise AssistantUnavailable("review_busy")
    if gate.owner_token:
        raise AssistantUnavailable("assistant_busy")
    token = secrets.token_hex(32)
    gate.owner_token = token
    gate.control_token = None
    gate.control_epoch = None
    gate.operation = operation
    gate.cancel_requested = False
    gate.heartbeat_at = now
    db.flush()
    return token


def checkpoint_upstream(db: Session, token: str, control_token: str, epoch: str | None = None,
                    *, operation='chat') -> None:
    """Commit before upstream submission, so a restart retains cancellation identity."""
    if (len(control_token) != 64 or any(c not in '0123456789abcdef' for c in control_token)
            or (epoch is not None and (not isinstance(epoch, str) or not 1 <= len(epoch) <= 64))):
        raise ValueError('invalid upstream control identity')
    gate = lock_gate(db)
    if (not gate.owner_token or not secrets.compare_digest(gate.owner_token, token)
            or operation not in {'chat', 'embedding'} or gate.operation != operation):
        raise AssistantUnavailable('assistant_ownership_lost')
    if gate.control_token is not None and gate.control_token != control_token:
        raise AssistantUnavailable('assistant_control_identity_changed')
    if gate.control_epoch is not None and gate.control_epoch != epoch:
        raise AssistantUnavailable('assistant_control_identity_changed')
    gate.control_token = control_token
    gate.control_epoch = epoch
    db.flush()


def request_preemption(db: Session) -> AssistantComputeGate:
    gate = lock_gate(db)
    if gate.owner_token:
        gate.cancel_requested = True
    db.flush()
    return gate


def heartbeat(db: Session, token: str, *, now=None, lease_seconds=30) -> bool:
    """False means stop forwarding work and begin upstream cancellation."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    gate = lock_gate(db)
    if not gate.owner_token or not secrets.compare_digest(gate.owner_token, token):
        return False
    if _expired(gate, now, lease_seconds):
        if not gate.fault:
            gate.fault = "heartbeat_expired; upstream idle confirmation required"
        gate.cancel_requested = True
    if review_pending(db):
        gate.cancel_requested = True
    if gate.fault or gate.cancel_requested:
        return False
    gate.heartbeat_at = now
    return True


def acknowledge_idle(db: Session, token: str) -> bool:
    """Internal only: call AFTER upstream ended or its idle probe succeeded.

Closing the browser, setting cancellation, and expiry are NOT idle evidence.
The token prevents a late old completion from releasing a newer operation.
"""
    gate = lock_gate(db)
    if not gate.owner_token or not secrets.compare_digest(gate.owner_token, token):
        return False
    gate.owner_token = None
    gate.control_token = None
    gate.control_epoch = None
    gate.operation = None
    gate.cancel_requested = False
    gate.heartbeat_at = None
    if not installer_recovery(gate):
        gate.fault = None
    return True


def fail_closed(db: Session, token: str, reason: str) -> None:
    gate = lock_gate(db)
    if gate.owner_token and secrets.compare_digest(gate.owner_token, token):
        gate.cancel_requested = True
        if not installer_recovery(gate):
            gate.fault = reason[:255]


def review_may_start(db: Session) -> bool:
    gate = request_preemption(db)
    return gate.owner_token is None and gate.fault is None


def recover_unsubmitted(db: Session, *, now=None, lease_seconds=30, operation='chat') -> bool:
    """Fence stale work that could not yet have passed the durable dispatch checkpoint."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    state = lock_gate(db)
    if (operation not in {'chat', 'embedding'} or state.operation != operation
            or not state.owner_token or state.control_epoch is not None
            or not (state.fault or _expired(state, now, lease_seconds))):
        return False
    # The legacy uncontrolled embedding path has no checkpoint at all. Absence
    # of an epoch alone is not idle evidence for that path.
    if operation == 'embedding' and state.control_token is None:
        return False
    # Keep the gate lock through revocation. A delayed gateway must successfully
    # commit its epoch with this owner token before submitting inference.
    released = acknowledge_idle(db, state.owner_token)
    db.flush()
    return released


def recovery_candidate(db: Session, *, now=None, lease_seconds=30, operation='chat'):
    """Fence a stale chat before returning its private cancellation identity."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    gate = lock_gate(db)
    if (operation not in {'chat', 'embedding'} or gate.operation != operation or not gate.owner_token
            or installer_recovery(gate) or not (gate.fault or _expired(gate, now, lease_seconds))):
        return None
    gate.cancel_requested = True
    gate.fault = operation + ' recovery awaiting upstream idle confirmation'
    if not gate.control_token or not gate.control_epoch:
        return None
    return gate.owner_token, gate.control_token, gate.control_epoch


def status(db: Session, *, now=None, lease_seconds=30) -> dict:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    gate = db.get(AssistantComputeGate, 1, populate_existing=True)
    if gate is None:
        return {"available": False, "reason": "scheduler_not_initialized"}
    if gate.fault or _expired(gate, now, lease_seconds):
        reason = "scheduler_recovery_required"
    elif review_pending(db, lock=False):
        reason = "review_busy"
    elif gate.owner_token:
        reason = "assistant_busy"
    else:
        reason = "idle"
    # Never expose the lease token or other users' task names in public status.
    return {"available": reason == "idle", "reason": reason,
            "cancel_requested": gate.cancel_requested, "operation": gate.operation}
