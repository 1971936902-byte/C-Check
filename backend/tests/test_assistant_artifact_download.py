import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('assistant_download',
    Path(__file__).resolve().parents[2] / 'deploy/assistant/download_verified.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
URL = 'https://example.test/pinned-artifact'
DATA = b'complete pinned artifact'
HASH = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def artifact_dir(tmp_path):
    path = tmp_path / 'downloads'
    path.mkdir()
    return path


def ranges(monkeypatch, *, corrupt=False, wrong_range=False):
    def open_range(request, timeout):
        first, last = map(int, request.get_header('Range').removeprefix('bytes=').split('-'))
        last = min(last, len(DATA) - 1)
        response = BytesIO((b'x' * len(DATA) if corrupt else DATA)[first:last + 1])
        response.status = 206
        response.headers = {'Content-Range': f'bytes {first + int(wrong_range)}-{last}/{len(DATA)}'}
        return response
    monkeypatch.setattr(module, 'urlopen', open_range)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)


def test_verified_ranges_publish_only_complete_artifact(artifact_dir, monkeypatch):
    tmp_path = artifact_dir
    ranges(monkeypatch)
    result = module.download(URL, HASH, tmp_path / 'artifact', chunk_size=5)
    assert result.read_bytes() == DATA
    assert list(tmp_path.iterdir()) == [result]


@pytest.mark.parametrize('kind', ['corrupt', 'wrong_range'])
def test_bad_ranges_never_publish_or_leave_partial_files(artifact_dir, monkeypatch, kind):
    tmp_path = artifact_dir
    ranges(monkeypatch, **{kind: True})
    with pytest.raises(ValueError):
        module.download(URL, HASH, tmp_path / 'artifact', chunk_size=5)
    assert list(tmp_path.iterdir()) == []


def test_existing_artifact_is_not_overwritten(artifact_dir):
    tmp_path = artifact_dir
    destination = tmp_path / 'artifact'
    destination.write_bytes(b'existing')
    with pytest.raises(ValueError, match='preserved'):
        module.download(URL, HASH, destination)
    assert destination.read_bytes() == b'existing'


def test_verified_existing_artifact_does_not_access_network(artifact_dir, monkeypatch):
    tmp_path = artifact_dir
    destination = tmp_path / 'artifact'
    destination.write_bytes(DATA)
    monkeypatch.setattr(module, 'urlopen', lambda *a, **k: pytest.fail('unexpected network request'))
    assert module.download(URL, HASH, destination) == destination


def test_artifact_creation_race_preserves_other_file(artifact_dir, monkeypatch):
    tmp_path = artifact_dir
    ranges(monkeypatch)
    original = module.urlopen
    destination = tmp_path / 'artifact'

    def concurrent_file(request, timeout):
        destination.write_bytes(b'other process')
        return original(request, timeout)

    monkeypatch.setattr(module, 'urlopen', concurrent_file)
    with pytest.raises(FileExistsError):
        module.download(URL, HASH, destination)
    assert destination.read_bytes() == b'other process'
    assert list(tmp_path.iterdir()) == [destination]


def test_curl_transport_handles_proxy_and_redirect_headers(artifact_dir, monkeypatch):
    def transfer(command, **kwargs):
        assert command[command.index('--proto') + 1] == '=https'
        assert command[command.index('--proto-redir') + 1] == '=https'
        assert '--insecure' not in command
        header = Path(command[command.index('--dump-header') + 1])
        body = Path(command[command.index('--output') + 1])
        header.write_bytes(b'HTTP/1.1 200 Connection established\r\n\r\n'
            b'HTTP/2 302\r\nLocation: https://example.test/asset\r\n\r\n'
            + f'HTTP/2 206\r\nContent-Range: bytes 0-{len(DATA)-1}/{len(DATA)}\r\n\r\n'.encode())
        body.write_bytes(DATA)
        return module.SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', transfer)
    result = module.download(URL, HASH, artifact_dir / 'artifact', transport='curl')
    assert result.read_bytes() == DATA


def test_sha512_artifact_integrity(artifact_dir, monkeypatch):
    ranges(monkeypatch)
    expected = hashlib.sha512(DATA).hexdigest()
    result = module.download(URL, expected, artifact_dir / 'artifact', algorithm='sha512', chunk_size=5)
    assert module.digest(result, 'sha512') == expected
