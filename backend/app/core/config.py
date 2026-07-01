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
    upload_max_archive_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    upload_max_extracted_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    upload_max_files: int = Field(default=200, gt=0)
    upload_max_archive_entries: int = Field(default=1000, gt=0)
    upload_max_path_length: int = Field(default=512, gt=0)
    review_max_source_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    cors_origins: list[str] = ["http://localhost:5173"]
    storage_path: Path = Path("uploads")
    mock_model_enabled: bool = False
    model_max_attempts: int = Field(default=3, ge=1, le=5)
    model_context_window: int = Field(default=12288, ge=1024, le=1048576)
    model_max_tokens: int = Field(default=2048, ge=256, le=16384)
    candidate_model_max_tokens: int = Field(default=2048, ge=256, le=16384)
    candidate_dynamic_tokens_enabled: bool = True
    candidate_dynamic_min_tokens: int = Field(default=768, ge=256, le=16384)
    candidate_dynamic_base_tokens: int = Field(default=384, ge=0, le=16384)
    candidate_dynamic_tokens_per_line: float = Field(default=4.5, ge=0.1, le=32.0)
    candidate_dynamic_tokens_per_function: int = Field(default=24, ge=0, le=256)
    candidate_dynamic_tokens_per_dangerous_op: int = Field(default=24, ge=0, le=256)
    candidate_dynamic_tokens_per_pointer_op: int = Field(default=4, ge=0, le=64)
    model_max_input_tokens: int = Field(default=10000, ge=512, le=1048576)
    model_token_chars_per_token: float = Field(default=3.5, ge=1.0, le=8.0)
    model_chunk_max_chars: int = Field(default=18000, ge=1000, le=200000)
    model_chunk_max_count: int = Field(default=360, ge=1, le=10000)
    model_chunk_concurrency: int = Field(default=1, ge=1, le=8)
    review_no_slice_max_bytes: int = Field(default=8 * 1024, ge=1024, le=1024 * 1024)
    model_small_task_max_files: int = Field(default=2, ge=1, le=1000)
    model_small_task_max_bytes: int = Field(default=128 * 1024, ge=1024, le=50 * 1024 * 1024)
    model_small_task_reserved_nodes: int = Field(default=1, ge=0, le=8)
    model_large_task_max_nodes: int = Field(default=2, ge=1, le=8)
    model_structured_outputs_enabled: bool = True
    rag_enabled: bool = True
    rag_retrieval_profile: str = "definition"
    rag_context_format: str = "segmented"
    rag_keyword_top_k: int = Field(default=8, ge=1, le=100)
    rag_graph_max_depth: int = Field(default=1, ge=0, le=3)
    rag_context_max_chars: int = Field(default=3000, ge=1000, le=100000)
    rag_embedding_backend: str = "hashing"
    rag_embedding_base_url: str | None = None
    rag_embedding_api_key: str | None = None
    rag_embedding_model: str = "hashing-code-embedding-v1"
    rag_embedding_dimension: int = Field(default=128, ge=16, le=4096)
    rag_embedding_timeout_seconds: int = Field(default=30, ge=1, le=300)
    rag_embedding_batch_size: int = Field(default=32, ge=1, le=128)
    rag_embedding_max_chars: int = Field(default=16000, ge=1000, le=100000)
    rag_embedding_allow_hash_fallback: bool = True
    rag_embedding_query_prefix: str = ""
    rag_embedding_passage_prefix: str = ""
    rag_qdrant_url: str | None = None
    rag_qdrant_api_key: str | None = None
    rag_qdrant_collection: str = "c_check_code_chunks"
    rag_cache_enabled: bool = True
    rag_parser_require_tree_sitter: bool = True
    rag_parser_require_libclang: bool = True
    rag_on_demand_enabled: bool = True
    rag_review_units_enabled: bool = False
    rag_candidate_scan_enabled: bool = True
    candidate_format_batch_size: int = Field(default=10, ge=1, le=100)
    candidate_format_model_enabled: bool = True
    rag_observability_enabled: bool = True
    model_catalog_path: Path = REPOSITORY_ROOT / "deploy" / "models" / "catalog.json"
    model_deployment_enabled: bool = False
    model_deployment_script: Path = REPOSITORY_ROOT / "deploy" / "models" / "deploy-vllm-model.sh"
    vllm_api_key: str | None = None
    allow_insecure_defaults: bool = False

    @model_validator(mode="after")
    def validate_deployment_settings(self) -> "Settings":
        if not self.storage_path.is_absolute():
            self.storage_path = REPOSITORY_ROOT / self.storage_path
        self.storage_path = self.storage_path.resolve()
        if not self.model_catalog_path.is_absolute():
            self.model_catalog_path = REPOSITORY_ROOT / self.model_catalog_path
        self.model_catalog_path = self.model_catalog_path.resolve()
        if not self.model_deployment_script.is_absolute():
            self.model_deployment_script = REPOSITORY_ROOT / self.model_deployment_script
        self.model_deployment_script = self.model_deployment_script.resolve()

        database_password = make_url(self.database_url).password
        insecure_fields = []
        if (
            database_password in INSECURE_DATABASE_PASSWORDS
            or not database_password
            or len(database_password) < 12
        ):
            insecure_fields.append("DATABASE_URL")
        if self.jwt_secret in INSECURE_JWT_SECRETS or len(self.jwt_secret) < 32:
            insecure_fields.append("JWT_SECRET")
        if self.admin_password in INSECURE_ADMIN_PASSWORDS or len(self.admin_password) < 12:
            insecure_fields.append("ADMIN_PASSWORD")
        if insecure_fields and not self.allow_insecure_defaults:
            fields = ", ".join(insecure_fields)
            raise ValueError(f"insecure placeholder values are forbidden for: {fields}")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
