import io
import json
from types import SimpleNamespace
import tarfile

import pytest

from app import backup


@pytest.mark.parametrize('with_assistant', [False, True])
def test_snapshot_includes_optional_knowledge_data_and_supports_legacy(tmp_path, monkeypatch, capsys, with_assistant):
    for name in backup.MEMBERS:
        if name in backup.OPTIONAL and not with_assistant:
            continue
        base = tmp_path / name
        base.mkdir(parents=True)
        (base / 'record').write_bytes(b'content')
    backup.measure(tmp_path)
    measurement = json.loads(capsys.readouterr().out)
    assert measurement['bytes'] == (6 if with_assistant else 5) * len(b'content')
    archive = io.BytesIO()
    with monkeypatch.context() as context:
        context.setattr(backup.sys, 'stdout', SimpleNamespace(buffer=archive))
        backup.create(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(archive.getvalue())) as result:
        names = result.getnames()
    assert ('data/assistant/record' in names) is with_assistant
    restored = tmp_path / 'restored'
    (restored / 'data/assistant').mkdir(parents=True)
    (restored / 'data/assistant/stale').write_bytes(b'stale')
    owners = []
    with monkeypatch.context() as context:
        context.setattr(backup.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(archive.getvalue())))
        context.setattr(backup.os, 'chown', lambda path, *args, **kwargs: owners.append(str(path)), raising=False)
        backup.restore(restored, 10001, 10001)
    assert not (restored / 'data/assistant/stale').exists()
    assert (restored / 'data/assistant/record').exists() is with_assistant
    if with_assistant:
        assert str(restored / 'data/assistant/record') in owners
