"""Engine-side completion proof for synchronous, bounded embedding batches."""
from dataclasses import dataclass
import re
import secrets
from threading import Condition

from fastapi import Header, HTTPException

TOKEN = re.compile(r'^[a-f0-9]{64}$')


@dataclass
class Reservation:
    started: bool = False
    cancelled: bool = False
    idle: bool = False


class Controller:
    def __init__(self, capacity=1024):
        self.epoch = secrets.token_hex(16)
        self.capacity = capacity
        self.requests = {}
        self.condition = Condition()

    def reserve(self, token):
        with self.condition:
            if not TOKEN.fullmatch(token) or token in self.requests or len(self.requests) >= self.capacity:
                raise ValueError('invalid or unavailable embedding reservation')
            self.requests[token] = Reservation()
            return self.epoch

    def state(self, token, epoch):
        if epoch != self.epoch or token not in self.requests:
            raise ValueError('unknown embedding reservation or engine epoch')
        return self.requests[token]

    def run(self, token, epoch, compute, synchronize):
        with self.condition:
            state = self.state(token, epoch)
            if state.started or state.cancelled:
                raise ValueError('embedding reservation is not admitted')
            state.started = True
        try:
            return compute()
        finally:
            # Even exception paths need an explicit CUDA completion barrier.
            # If synchronization fails, leave the reservation fenced, not idle.
            synchronize()
            with self.condition:
                state.idle = True
                self.condition.notify_all()

    def cancel(self, token, epoch, timeout=5):
        with self.condition:
            state = self.state(token, epoch)
            state.cancelled = True
            if not state.started:
                state.idle = True
            return self.condition.wait_for(lambda: state.idle, timeout=timeout)

    def forget(self, token, epoch):
        with self.condition:
            state = self.state(token, epoch)
            if not state.cancelled or not state.idle:
                raise ValueError('embedding completion is not confirmed')
            del self.requests[token]


def install(app, *, original, body_type, synchronize, control_key):
    if len(control_key) < 32:
        raise ValueError('dedicated control key must have at least 32 characters')
    controller = Controller()

    def authenticate(value):
        if not value or not secrets.compare_digest(value.encode(), control_key.encode()):
            raise HTTPException(401, 'invalid embedding control credentials')

    @app.post('/internal/assistant/{token}/reserve')
    def reserve(token: str, x_ccheck_control_key: str | None = Header(default=None)):
        authenticate(x_ccheck_control_key)
        try:
            return {'epoch': controller.reserve(token)}
        except ValueError:
            raise HTTPException(409, 'embedding reservation unavailable') from None

    @app.post('/internal/assistant/{token}/embeddings')
    def embeddings(token: str, epoch: str, body: dict,
                   authorization: str | None = Header(default=None),
                   x_ccheck_control_key: str | None = Header(default=None)):
        authenticate(x_ccheck_control_key)
        try:
            parsed = body_type.model_validate(body)
            return controller.run(token, epoch, lambda: original(parsed, authorization), synchronize)
        except ValueError:
            raise HTTPException(409, 'embedding request not admitted or invalid') from None

    @app.post('/internal/assistant/{token}/cancel')
    def cancel(token: str, epoch: str, x_ccheck_control_key: str | None = Header(default=None)):
        authenticate(x_ccheck_control_key)
        try:
            return {'epoch': controller.epoch, 'idle': controller.cancel(token, epoch)}
        except ValueError:
            raise HTTPException(409, 'embedding recovery identity mismatch') from None

    @app.delete('/internal/assistant/{token}')
    def forget(token: str, epoch: str, x_ccheck_control_key: str | None = Header(default=None)):
        authenticate(x_ccheck_control_key)
        try:
            controller.forget(token, epoch)
            return {'released': True}
        except ValueError:
            raise HTTPException(409, 'embedding completion not confirmed') from None
    return controller
