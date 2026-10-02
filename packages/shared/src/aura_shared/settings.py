"""Typed settings loaded from environment variables prefixed with ``AURA_``."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AURA_", env_file=".env", extra="ignore")

    env: Environment = Environment.DEV
    region: str = "in"

    database_url: str = "postgresql+asyncpg://aura:aura@localhost:5432/aura"

    # Redis is split by workload so eviction in one role never affects another (ADR-0002).
    redis_state_url: str = "redis://localhost:6379/0"
    redis_cache_url: str = "redis://localhost:6379/1"
    redis_vector_url: str = "redis://localhost:6379/2"
    redis_max_connections: int = 64
    redis_socket_timeout_s: float = 1.0

    nats_url: str = "nats://localhost:4222"
    clickhouse_url: str = "http://localhost:8123"
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "aura-dev"

    master_key: SecretStr | None = None

    # --- auth ---
    public_web_url: str = "http://localhost:3000"
    public_admin_url: str = "http://localhost:3001"
    public_api_url: str = "http://localhost:8000"
    session_cookie_name: str = "aura_session"
    session_ttl_s: int = 60 * 60 * 24 * 14
    cookie_secure: bool = False
    api_key_pepper: SecretStr = SecretStr("dev-only-pepper-change-me")
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None

    # --- mail ---
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mail_from: str = "AURA <no-reply@aura.local>"
    mail_backend: Literal["smtp", "log"] = "smtp"  # "log" is honoured in dev/test only

    # --- billing (Razorpay) ---
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None

    # --- runtime (data plane) ---
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    # e.g. http://localhost:11434/v1 for Ollama. Unset = no self-hosted models.
    ollama_base_url: str | None = None

    # --- app chat: multi-agent answering (Drafter ↔ Reviewer → Finaliser) ---
    chat_agents: bool = True  # kill switch: False = one routed answer per message
    chat_reviewer_model: str = "groq/openai/gpt-oss-20b"
    chat_agent_max_depth: int = 3
    chat_agent_max_turns: int = 3
    chat_agent_max_nodes: int = 12
    chat_agent_max_calls: int = 40
    chat_agent_max_wall_ms: int = 120_000
    # Sub-query decomposition style: False = sub-questions (default), True = sub-tasks
    subquery_as_tasks: bool = False
    # Deterministic stub models; only honoured in dev/test environments.
    stub_provider: bool = False
    nats_enabled: bool = False
    runtime_rate_per_key_per_s: float = 20.0
    runtime_burst_per_key: int = 40
    runtime_timeout_s: float = 300.0
    runtime_max_request_bytes: int = 8_000_000
    # Semantic cache embeddings: "hashing" (local, lexical) or "openai" (any
    # OpenAI-compatible /embeddings endpoint, using openai_api_key).
    embedding_backend: str = "hashing"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_base_url: str = "https://api.openai.com/v1"
    # Graph tools (enabled only when configured).
    brave_api_key: SecretStr | None = None
    sandbox_url: str | None = None
    sandbox_api_key: SecretStr | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
