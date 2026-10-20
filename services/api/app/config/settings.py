"""Application configuration, loaded from environment variables.

All values are placeholders until verified against the real deployment
(see docs/backlog.md, "Remaining assumptions"). Nothing here is sent to clients.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: `MODEL_MAX_OUTPUT_TOKENS=` in .env means "unset", not an empty string.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", protected_namespaces=(), env_ignore_empty=True
    )

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

    # Chat and runs
    default_system_prompt: str = "You are a helpful assistant."
    context_safety_margin_tokens: int = Field(default=256, ge=0)
    run_lease_seconds: int = Field(default=30, ge=5)
    run_max_attempts: int = Field(default=2, ge=1)
    run_delta_flush_ms: int = Field(default=100, ge=10)
    worker_poll_interval_seconds: float = Field(default=0.5, gt=0)
    sse_poll_interval_seconds: float = Field(default=0.25, gt=0)
    sse_keepalive_seconds: float = Field(default=15, gt=0)

    # Built web UI (apps/web/dist); served by the API when the directory exists.
    static_root: str | None = None

    # Limits
    max_upload_bytes: int = 50 * 1024 * 1024
    max_user_storage_bytes: int = 10 * 1024 * 1024 * 1024

    # Files
    upload_session_ttl_minutes: int = Field(default=60, ge=1)
    max_extracted_text_chars: int = Field(default=500_000, ge=1000)
    # Most of one attachment's text that goes into a prompt. Longer documents are sent as
    # their beginning plus the passages most relevant to the question; tools can read the
    # rest from the workspace.
    max_attachment_prompt_chars: int = Field(default=60_000, ge=1000)
    # Share of the context window the newest message's attachments may use together.
    attachment_context_share: float = Field(default=0.4, ge=0.05, le=0.8)
    # Excerpt kept from files attached to earlier messages (already answered).
    attachment_history_chars: int = Field(default=3000, ge=0)
    # Share of the context budget that project knowledge files may use.
    project_files_context_share: float = Field(default=0.3, ge=0, le=0.9)
    archive_max_entries: int = Field(default=10_000, ge=1)
    archive_max_total_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    archive_max_ratio: int = Field(default=100, ge=1)
    image_max_pixels: int = Field(default=50_000_000, ge=1)
    vision_max_side: int = Field(default=1024, ge=64)
    # Optional malware scanning with clamd (ClamAV). Unset = no scanning.
    clamav_host: str | None = None
    clamav_port: int = 3310
    job_parallel: int = Field(default=2, ge=1)
    job_lease_seconds: int = Field(default=600, ge=30)
    job_max_attempts: int = Field(default=2, ge=1)

    # Memory and recall (ADR 0009). These are defaults; each user can change their own.
    memory_enabled: bool = True
    # After each reply, ask the model what is worth remembering (uses inference time).
    memory_auto_extraction: bool = True
    # Proposed memories wait in a review queue until approved.
    memory_require_review: bool = True
    # Embeddings (optional; keyword search is used without them). An OpenAI-compatible
    # /v1/embeddings endpoint, e.g. a second llama-server started with --embeddings.
    embedding_base_url: str | None = None  # defaults to MODEL_BASE_URL
    embedding_model: str | None = None
    embedding_api_key: SecretStr | None = None
    # How much memory goes into a prompt.
    memory_prompt_max_items: int = Field(default=30, ge=0, le=200)
    memory_recall_chunks: int = Field(default=4, ge=0, le=20)
    memory_context_share: float = Field(default=0.15, ge=0, le=0.5)
    memory_extraction_max_per_turn: int = Field(default=3, ge=0, le=10)

    # Tools (ADR 0008)
    # Profile for conversations that don't pick one (users can change their own default).
    tool_default_profile: Literal["read_only", "standard", "developer", "administrator"] = (
        "standard"
    )
    # The sandbox service (services/sandbox). Unset: Python, shell, Git and the document
    # and image tools are not offered at all.
    sandbox_url: str | None = None
    sandbox_token: SecretStr | None = None
    # Code in the sandbox may use the internet through the egress proxy (public addresses
    # only; never the LAN). Off: the sandbox has no network at all.
    tool_sandbox_network: bool = True
    # Web access (web_fetch, git_clone) from the worker, through the SSRF guard.
    tool_network_enabled: bool = True
    # Comma-separated host suffixes (e.g. "github.com,python.org"). Empty: any public host.
    tool_web_allowlist: str = ""
    tool_web_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    tool_max_iterations: int = Field(default=12, ge=1, le=100)
    tool_max_run_seconds: int = Field(default=900, ge=10)
    tool_call_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    tool_memory_mb: int = Field(default=1024, ge=64)
    tool_max_result_chars: int = Field(default=20_000, ge=500)
    tool_approval_ttl_minutes: int = Field(default=60, ge=1)
    mcp_timeout_seconds: float = Field(default=30, gt=0)
    schedule_min_interval_minutes: int = Field(default=15, ge=1)
    schedule_max_per_user: int = Field(default=20, ge=0)

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
    def web_allowlist(self) -> list[str]:
        hosts = (h.strip().lower().lstrip(".") for h in self.tool_web_allowlist.split(","))
        return [h for h in hosts if h]

    @property
    def cookie_secure(self) -> bool:
        if self.app_cookie_secure is not None:
            return self.app_cookie_secure
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
