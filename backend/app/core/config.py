from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INSECURE_JWT_SECRETS = {
    "development-only-change-me",
    "replace-with-a-long-random-secret",
    "CHANGE_ME_USE_A_LONG_RANDOM_SECRET",
}
INSECURE_ADMIN_PASSWORDS = {"change-this-password", "CHANGE_ME_USE_A_STRONG_ADMIN_PASSWORD"}
INSECURE_DATABASE_PASSWORDS = {"c_check_password", "CHANGE_ME_USE_A_STRONG_DATABASE_PASSWORD"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPOSITORY_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "mysql+pymysql://c_check:c_check_password@localhost:3306/c_check"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "development-only-change-me"
    jwt_expire_minutes: int = Field(default=480, gt=0)
    admin_username: str = "admin"
    admin_password: str = "change-this-password"
    upload_max_file_bytes: int = Field(default=1024 * 1024, gt=0)
    upload_max_extracted_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    upload_max_files: int = Field(default=200, gt=0)
    cors_origins: list[str] = ["http://localhost:5173"]
    storage_path: Path = Path("uploads")
    mock_model_enabled: bool = False
    allow_insecure_defaults: bool = False

    @model_validator(mode="after")
    def validate_deployment_settings(self) -> "Settings":
        if not self.storage_path.is_absolute():
            self.storage_path = REPOSITORY_ROOT / self.storage_path
        self.storage_path = self.storage_path.resolve()

        insecure_fields = []
        if make_url(self.database_url).password in INSECURE_DATABASE_PASSWORDS:
            insecure_fields.append("DATABASE_URL")
        if self.jwt_secret in INSECURE_JWT_SECRETS:
            insecure_fields.append("JWT_SECRET")
        if self.admin_password in INSECURE_ADMIN_PASSWORDS:
            insecure_fields.append("ADMIN_PASSWORD")
        if insecure_fields and not self.allow_insecure_defaults:
            fields = ", ".join(insecure_fields)
            raise ValueError(f"insecure placeholder values are forbidden for: {fields}")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
