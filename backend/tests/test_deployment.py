from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings


def test_deployed_model_name_defaults_to_legacy(monkeypatch):
    from app.deployment import review_model
    monkeypatch.delenv('CCHECK_REVIEW_MODEL', raising=False)
    assert review_model() == ('qwen2.5-coder-32b-instruct-fp8', 'Qwen2.5-Coder-32B FP8')


def test_deployed_model_name_accepts_qwen3_and_rejects_unknown(monkeypatch):
    from app.deployment import review_model
    monkeypatch.setenv('CCHECK_REVIEW_MODEL', 'qwen3-coder-30b-a3b-instruct-fp8')
    assert review_model() == ('qwen3-coder-30b-a3b-instruct-fp8', 'Qwen3-Coder-30B-A3B FP8')
    monkeypatch.setenv('CCHECK_REVIEW_MODEL', 'unvalidated-model')
    with pytest.raises(RuntimeError, match='unsupported'):
        review_model()


@pytest.mark.parametrize('name', ['database_url', 'jwt_secret', 'admin_password',
                                  'rag_embedding_api_key', 'rag_qdrant_api_key', 'vllm_api_key'])
def test_secret_file_loading_without_host_installation(tmp_path, monkeypatch, name):
    monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv(name.upper() + '_FILE', raising=False)
    secret = 'mysql+pymysql://user:long-random-test-password@db/project' if name == 'database_url' else 'test-secret-value-longer-than-32-characters'
    path = tmp_path / 'secret'
    path.write_text(secret + '\n', encoding='utf-8')
    settings = Settings(_env_file=None, allow_insecure_defaults=True, **{name + '_file': path})
    assert getattr(settings, name) == secret


def test_secret_file_conflict_is_explicit_and_does_not_print_secret(tmp_path):
    path = tmp_path / 'secret'
    path.write_text('private-test-material-1234567890')
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, allow_insecure_defaults=True, jwt_secret='must-not-appear-in-errors', jwt_secret_file=path)
    assert 'only one' in str(error.value)
    assert 'must-not-appear-in-errors' not in str(error.value)


@pytest.mark.parametrize('content', ['', '\n', 'abc\x00def'])
def test_invalid_secret_file_is_rejected(tmp_path, monkeypatch, content):
    monkeypatch.delenv('JWT_SECRET', raising=False)
    path = tmp_path / 'secret'
    path.write_text(content)
    with pytest.raises(ValidationError, match='empty or invalid'):
        Settings(_env_file=None, allow_insecure_defaults=True, jwt_secret_file=path)


def test_maintenance_blocks_admission_before_database_writes(tmp_path, monkeypatch):
    from app.api.reviews import _create_task
    maintenance = tmp_path / 'maintenance'
    maintenance.touch()
    settings = Settings(_env_file=None, allow_insecure_defaults=True, deployment_maintenance_file=maintenance)
    monkeypatch.setattr('app.api.reviews.get_settings', lambda: settings)
    database = Mock()
    with pytest.raises(HTTPException) as error:
        _create_task(database, Mock(), 'model-id', Mock(), ['buffer_overflow'])
    assert error.value.status_code == 503
    database.add.assert_not_called()


def test_backup_paths_do_not_allow_escape_or_model_copies():
    from app.backup import permitted
    assert permitted('data/uploads/file.c')
    assert permitted('secrets/admin_password')
    assert not permitted('data/mysql/../../../etc/passwd')
    assert not permitted('/data/uploads/file.c')
    assert not permitted('models/qwen/model.safetensors')
    assert not permitted('data/uploads\\..\\etc/passwd')
    assert permitted('data/assistant/storage/anythingllm.db')


def test_maintenance_pending_cancels_chat_and_counts_document_writers(db_session_factory, monkeypatch, capsys):
    import json
    from app import deployment
    from app.db.models import AssistantComputeGate, AssistantDocument, User
    from app.services import assistant_gate
    monkeypatch.setenv('ASSISTANT_ENABLED', 'true')
    monkeypatch.setattr(deployment, 'SessionLocal', db_session_factory)
    with db_session_factory() as db:
        db.add(AssistantComputeGate(id=1, generation=0, cancel_requested=False))
        user = User(username='backup-user', password_hash='unused')
        db.add(user)
        db.flush()
        for status in ('queued', 'processing', 'ready', 'paused'):
            db.add(AssistantDocument(owner_id=user.id, filename=status + '.txt', size_bytes=1,
                sha256='a' * 64, status=status))
        assistant_gate.claim(db, 'chat')
        db.commit()
    deployment.pending()
    assert json.loads(capsys.readouterr().out)['pending'] == 3
    with db_session_factory() as db:
        assert db.get(AssistantComputeGate, 1).cancel_requested
