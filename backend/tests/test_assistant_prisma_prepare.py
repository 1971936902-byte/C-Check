import gzip
import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy/assistant'))
import prepare_prisma
import prepare_sharp


@pytest.fixture
def artifacts(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir()
    engines = []
    for name in ('libquery_engine.so.node', 'schema-engine'):
        data = gzip.compress(('test engine ' + name).encode())
        archive = name + '.gz'
        (cache / archive).write_bytes(data)
        engines.append({'name': name, 'archive': archive, 'sha256': hashlib.sha256(data).hexdigest()})
    return cache, tmp_path / 'engines', {'engines': engines, 'commit': 'test', 'platform': 'test'}


def test_prepare_uses_verified_cache_without_download(artifacts, monkeypatch):
    cache, output, lock = artifacts
    import download_verified
    monkeypatch.setattr(download_verified, 'curlopen', lambda *a, **k: pytest.fail('cache should avoid network'))
    evidence = prepare_prisma.prepare(cache, output, lock)
    for engine in lock['engines']:
        binary = output / engine['name']
        assert binary.read_bytes() == ('test engine ' + engine['name']).encode()
        assert evidence[engine['name']]['archive_sha256'] == engine['sha256']
        assert evidence[engine['name']]['binary_sha256'] == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_corrupted_cache_does_not_publish_engines(artifacts):
    cache, output, lock = artifacts
    (cache / lock['engines'][1]['archive']).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='preserved'):
        prepare_prisma.prepare(cache, output, lock)
    assert not output.exists()


def test_existing_engine_directory_is_preserved(artifacts):
    cache, output, lock = artifacts
    output.mkdir()
    (output / 'existing').write_bytes(b'keep')
    with pytest.raises(ValueError, match='new'):
        prepare_prisma.prepare(cache, output, lock)
    assert (output / 'existing').read_bytes() == b'keep'


def test_archive_traversal_is_rejected(artifacts):
    cache, output, lock = artifacts
    lock['engines'][0]['archive'] = '../elsewhere.gz'
    with pytest.raises(ValueError, match='archive name'):
        prepare_prisma.prepare(cache, output, lock)
    assert not output.exists()


def test_libvips_archive_is_copied_only_after_integrity_check(tmp_path):
    cache = tmp_path / 'native-cache'
    cache.mkdir()
    filename = 'libvips-8.14.5-linux-x64.tar.br'
    content = b'test archive'
    (cache / filename).write_bytes(content)
    lock = {'filename': filename, 'libvips_version': '8.14.5',
            'sha512': hashlib.sha512(content).hexdigest(), 'url': 'https://example.test/libvips'}
    output = tmp_path / 'libvips'
    result = prepare_sharp.prepare(cache, output, lock)
    assert result['sha512'] == lock['sha512']
    assert (output / 'v8.14.5' / filename).read_bytes() == content
    (cache / filename).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='preserved'):
        prepare_sharp.prepare(cache, tmp_path / 'bad-libvips', lock)
    assert not (tmp_path / 'bad-libvips').exists()
