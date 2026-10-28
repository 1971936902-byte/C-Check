from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

source = Path(__file__).resolve().parents[2] / 'deploy/assistant/embedding_control.py'
spec = importlib.util.spec_from_file_location('assistant_embedding_control_test', source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
TOKEN = 'a' * 64


def test_cancel_waits_for_gpu_synchronization_not_just_python_return():
    controller = module.Controller()
    epoch = controller.reserve(TOKEN)
    syncing, allowed = Event(), Event()
    def synchronize():
        syncing.set()
        assert allowed.wait(2)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(controller.run, TOKEN, epoch, lambda: 'vectors', synchronize)
        assert syncing.wait(2)
        assert not controller.cancel(TOKEN, epoch, timeout=0)
        with pytest.raises(ValueError):
            controller.forget(TOKEN, epoch)
        allowed.set()
        assert work.result(timeout=2) == 'vectors'
    assert controller.cancel(TOKEN, epoch, timeout=0)
    controller.forget(TOKEN, epoch)
    with pytest.raises(ValueError):
        controller.run(TOKEN, epoch, lambda: pytest.fail('late admission'), lambda: None)


def test_cancel_before_admission_fences_delayed_request():
    controller = module.Controller()
    epoch = controller.reserve(TOKEN)
    assert controller.cancel(TOKEN, epoch, timeout=0)
    with pytest.raises(ValueError):
        controller.run(TOKEN, epoch, lambda: pytest.fail('cancelled admission'), lambda: None)


def test_synchronization_failure_never_proves_idle():
    controller = module.Controller()
    epoch = controller.reserve(TOKEN)
    def broken():
        raise RuntimeError('device lost')
    with pytest.raises(RuntimeError):
        controller.run(TOKEN, epoch, lambda: 'vectors', broken)
    assert not controller.cancel(TOKEN, epoch, timeout=0)


def test_compute_failure_can_release_only_after_successful_barrier():
    controller = module.Controller()
    epoch = controller.reserve(TOKEN)
    synchronized = []
    def broken():
        raise RuntimeError('invalid model output')
    with pytest.raises(RuntimeError):
        controller.run(TOKEN, epoch, broken, lambda: synchronized.append(True))
    assert synchronized == [True]
    assert controller.cancel(TOKEN, epoch, timeout=0)


def test_control_routes_require_separate_credentials_and_epoch():
    class Body(BaseModel):
        input: list[str]
    app = FastAPI()
    original_calls = []
    controller = module.install(app, original=lambda body, auth: original_calls.append(auth) or {'data': body.input},
        body_type=Body, synchronize=lambda: None, control_key='c' * 32)
    with TestClient(app) as client:
        path = '/internal/assistant/' + TOKEN
        assert client.post(path + '/reserve').status_code == 401
        headers = {'X-CCheck-Control-Key': 'c' * 32, 'Authorization': 'Bearer embedding-key'}
        epoch = client.post(path + '/reserve', headers=headers).json()['epoch']
        assert epoch == controller.epoch
        assert client.post(path + '/embeddings?epoch=wrong', headers=headers, json={'input': ['x']}).status_code == 409
        assert original_calls == []
        response = client.post(path + '/embeddings', params={'epoch': epoch}, headers=headers, json={'input': ['x']})
        assert response.status_code == 200 and response.json() == {'data': ['x']}
        assert original_calls == ['Bearer embedding-key']
        assert client.post(path + '/cancel', params={'epoch': epoch}, headers=headers).json()['idle'] is True
