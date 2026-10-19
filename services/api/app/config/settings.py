"""Application configuration, loaded from environment variables.

All values are placeholders until verified against the real deployment
(see docs/backlog.md, "Remaining assumptions"). Nothing here is sent to clients.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", protected_namespaces=())

    # Application
    app_env: Literal["development", "test", "production"] = "development"
    app_public_url: str = "http://localhost:8000"
    app_secret_key: SecretStr = SecretStr("dev-insecure-secret-change-me")
    app_single_user_mode: bool = True
    app_session_ttl_hours: int = 24 * 14
    app_cookie_secure: bool | None = None  # defaults to True in production

    # Bootstrap account (single-user mode). Only used to seed the first user.
    bootstrap_username: str | None = None
    bootstrap_password: SecretStr | None = None

    # Persistence
    database_url: str = "postgresql+asyncpg://celius:celius@localhost:5432/celius"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: str = "/data/app"
    workspace_root: str = "/data/workspaces"

    # Model gateway
    model_provider: Literal["llama_cpp", "openai_compatible"] = "llama_cpp"
    model_base_url: str = "http://localhost:8080/v1"
    model_api_key: SecretStr | None = None
    model_default_id: str = "local-model"
    model_connect_timeout_seconds: float = 10
    model_read_timeout_seconds: float = 300
    model_max_retries: int = Field(default=1, ge=0, le=5)
    model_default_context_tokens: int | None = None
    model_max_output_tokens: int | None = None
    model_parallel_requests: int = Field(default=1, ge=1)
    model_enable_tool_calling: bool = True
    model_enable_vision: bool = False
    model_enable_embeddings: bool = False

    # Limits
    max_upload_bytes: int = 50 * 1024 * 1024
    max_user_storage_bytes: int = 10 * 1024 * 1024 * 1024

    # Memory
    memory_enabled: bool = True
    memory_auto_extraction: bool = True
    memory_require_review: bool = True

    # Tools
    tool_default_profile: Literal["read_only", "standard", "developer", "administrator"] = (
        "read_only"
    )
    tool_sandbox_required: bool = True
    tool_default_network_access: bool = False

    # Backups
    backup_root: str = "/data/backups"
    backup_retention_days: int = 30

    @model_validator(mode="after")
    def _check_production_secrets(self) -> "Settings":
        if self.app_env == "production":
            secret = self.app_secret_key.get_secret_value()
            if secret.startswith("dev-insecure") or len(secret) < 32:
                raise ValueError("APP_SECRET_KEY must be a random value of at least 32 characters")
        return self

    @property
    def cookie_secure(self) -> bool:
        if self.app_cookie_secure is not None:
            return self.app_cookie_secure
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
