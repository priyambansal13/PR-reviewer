"""Settings shared by every service, read from environment variables (and `.env` when running on the host)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Infrastructure
    database_url: str = "postgresql+asyncpg://reviewer:reviewer@localhost:5435/reviewer"
    redis_url: str = "redis://localhost:6380/0"

    # GitHub App (Phase 2)
    github_app_id: str = ""
    github_private_key_path: Path = Path("secrets/github-app.pem")
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"

    # Internal routing. The gateway sends INTERNAL_TOKEN so internal services can tell its calls apart.
    webhook_service_url: str = "http://localhost:8001"
    internal_token: str = ""

    # LLM (Phase 3)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Guard-rails
    max_diff_lines: int = 3000
    max_inline_comments: int = 25
    daily_token_budget: int = 2_000_000

    # Langfuse (Phase 6) — tracing is off when the keys are blank
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
