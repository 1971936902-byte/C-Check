import importlib.util
import json
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[2] / 'deploy/assistant/probe_qwen_gpu.py'
spec = importlib.util.spec_from_file_location('assistant_gpu_probe', path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def snapshot(identifier, health='healthy', running=True):
    return {'Id': identifier, 'State': {'Running': running, 'Health': {'Status': health}}}


def test_restoration_waits_for_both_original_healthchecks(monkeypatch):
    results = iter([snapshot('q', 'starting'), snapshot('j'), snapshot('q'), snapshot('j')])
    calls, sleeps = [], []
    def inspect(*args):
        calls.append(args[-1])
        return json.dumps([next(results)])
    monkeypatch.setattr(probe, 'run', inspect)
    monkeypatch.setattr(probe.time, 'sleep', sleeps.append)
    probe.wait_for_restored_inference(['qwen', 'jina'], [snapshot('q'), snapshot('j')])
    assert calls == ['qwen', 'jina', 'qwen', 'jina']
    assert sleeps == [3]


@pytest.mark.parametrize('state', [snapshot('replacement'), snapshot('q', running=False)])
def test_restoration_rejects_replaced_or_stopped_original(monkeypatch, state):
    monkeypatch.setattr(probe, 'run', lambda *args: json.dumps([state]))
    with pytest.raises(RuntimeError, match='identity or stopped'):
        probe.wait_for_restored_inference(['qwen'], [snapshot('q')])


def test_restoration_starting_is_not_success(monkeypatch):
    monkeypatch.setattr(probe, 'run', lambda *args: json.dumps([snapshot('q', 'starting')]))
    with pytest.raises(RuntimeError, match='health recovery timed out'):
        probe.wait_for_restored_inference(['qwen'], [snapshot('q')], timeout=0)
