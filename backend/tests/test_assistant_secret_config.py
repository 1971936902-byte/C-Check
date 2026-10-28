import pytest
from pydantic import ValidationError, SecretStr

from app.core.config import Settings


@pytest.mark.parametrize('name', ['assistant_gateway_key', 'assistant_qwen_api_key', 'assistant_control_key', 'assistant_anythingllm_api_key'])
def test_assistant_secrets_support_file_mounts_and_are_redacted(tmp_path, name):
    file = tmp_path / 'key'
    file.write_text('private-test-value\n')
    settings = Settings(_env_file=None, allow_insecure_defaults=True, **{name + '_file': file})
    value = getattr(settings, name)
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == 'private-test-value'
    assert 'private-test-value' not in settings.model_dump_json()
    with pytest.raises(ValidationError, match='configure only one'):
        Settings(_env_file=None, allow_insecure_defaults=True, **{name: 'duplicate', name + '_file': file})
