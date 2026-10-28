from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy/assistant'))
import probe_anythingllm_image as probe


def test_command_has_bounded_default_and_override(monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=' ok\n')

    monkeypatch.setattr(probe.subprocess, 'run', execute)
    assert probe.run('docker', 'inspect') == 'ok'
    probe.run('docker', 'exec', timeout=180)
    assert [call['timeout'] for call in calls] == [30, 180]
    assert all(call['check'] for call in calls)


@pytest.mark.parametrize('timed_out', [False, True])
def test_failed_exec_output_is_included_and_secrets_redacted(tmp_path, timed_out):
    tmp_path = tmp_path / 'secrets'
    tmp_path.mkdir()
    (tmp_path / 'key').write_text('private-probe-key\n')
    (tmp_path / 'empty').write_text('')
    if timed_out:
        error = subprocess.TimeoutExpired('docker', 180,
            output=b'partial private-probe-key', stderr=b'assertion failed')
    else:
        error = subprocess.CalledProcessError(1, 'docker',
            output='partial private-probe-key', stderr='assertion failed')
    result = probe.safe_diagnostic(error, 'logs private-probe-key', tmp_path)
    assert 'assertion failed' in result
    assert 'partial' in result
    assert 'private-probe-key' not in result
    assert result.count('[redacted]') == 2


def test_diagnostics_remain_bounded(tmp_path):
    tmp_path = tmp_path / 'secrets'
    tmp_path.mkdir()
    assert len(probe.safe_diagnostic(ValueError(), 'x' * 20000, tmp_path)) == 12000
