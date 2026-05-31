from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import REPOSITORY_ROOT, Settings


def test_settings_enable_mock_model_only_when_explicit(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULTS", "true")
    monkeypatch.setenv("MOCK_MODEL_ENABLED", "true")

    assert Settings().mock_model_enabled is True


def test_settings_disable_mock_model_by_default(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULTS", "true")
    monkeypatch.delenv("MOCK_MODEL_ENABLED", raising=False)

    assert Settings().mock_model_enabled is False


def test_settings_reject_insecure_credentials_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULTS", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="insecure placeholder"):
        Settings(_env_file=None)


def test_settings_allow_insecure_credentials_only_when_explicit(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULTS", "true")

    settings = Settings(_env_file=None)

    assert settings.database_url == "mysql+pymysql://c_check:c_check_password@localhost:3306/c_check"
    assert settings.jwt_secret == "development-only-change-me"
    assert settings.admin_password == "change-this-password"


def test_settings_reject_default_database_password_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULTS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("JWT_SECRET", "production-jwt-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "production-admin-password")

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None)


def test_settings_reject_example_file_placeholders(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULTS", raising=False)
    monkeypatch.setenv("JWT_SECRET", "CHANGE_ME_USE_A_LONG_RANDOM_SECRET")
    monkeypatch.setenv("ADMIN_PASSWORD", "CHANGE_ME_USE_A_STRONG_ADMIN_PASSWORD")

    with pytest.raises(ValidationError, match="insecure placeholder"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("environment", "expected_field"),
    [
        (
            {
                "DATABASE_URL": "mysql+pymysql://c_check:strong-database-password@localhost/c_check",
                "JWT_SECRET": "short-secret",
                "ADMIN_PASSWORD": "strong-admin-password",
            },
            "JWT_SECRET",
        ),
        (
            {
                "DATABASE_URL": "mysql+pymysql://c_check:strong-database-password@localhost/c_check",
                "JWT_SECRET": "production-jwt-secret-at-least-32-characters",
                "ADMIN_PASSWORD": "too-short",
            },
            "ADMIN_PASSWORD",
        ),
        (
            {
                "DATABASE_URL": "mysql+pymysql://c_check:short@localhost/c_check",
                "JWT_SECRET": "production-jwt-secret-at-least-32-characters",
                "ADMIN_PASSWORD": "strong-admin-password",
            },
            "DATABASE_URL",
        ),
    ],
)
def test_settings_reject_weak_credentials(monkeypatch, environment, expected_field):
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULTS", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=expected_field):
        Settings(_env_file=None)


def test_settings_resolve_relative_storage_path_from_repository_root(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULTS", "true")
    monkeypatch.setenv("STORAGE_PATH", "custom-uploads")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert settings.storage_path == REPOSITORY_ROOT / "custom-uploads"
    assert settings.storage_path.is_absolute()


def test_settings_preserve_absolute_storage_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULTS", "true")
    storage_path = tmp_path / "uploads"
    monkeypatch.setenv("STORAGE_PATH", str(storage_path))

    assert Settings(_env_file=None).storage_path == storage_path


def test_settings_locate_dotenv_from_repository_root():
    assert Settings.model_config["env_file"] == (REPOSITORY_ROOT / ".env",)
    assert Path(Settings.model_config["env_file"][0]).is_absolute()
