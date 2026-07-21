"""Application settings loaded from environment variables and .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized application settings.

    All values are loaded from environment variables or the .env file.
    Use `get_settings()` to obtain a cached singleton instance.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "Niki"
    debug: bool = False
    log_level: str = "INFO"

    # --- API Server ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_version: str = "v1"

    # --- DeepSeek-V4 (deep reasoning — child-facing output generation) ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout: int = 30  # seconds
    deepseek_max_retries: int = 3

    # --- doubao (lightweight — classification / checking / preprocessing) ---
    doubao_api_key: str = ""
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_model: str = "doubao-lite-32k"
    doubao_timeout: int = 15  # seconds
    doubao_max_retries: int = 3

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 20
    redis_key_prefix: str = "niki:"

    # --- ChromaDB (Vector Store) ---
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    chroma_persist_dir: str = "./chroma_data"

    # --- Neo4j (Cognitive Graph) ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "nikidev123"

    # --- PostgreSQL ---
    database_url: str = "postgresql+asyncpg://niki:nikidev123@localhost:5432/niki"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False  # SQL query logging

    # --- Security ---
    aes_encryption_key: str = ""
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"

    # --- Session defaults ---
    default_daily_limit_minutes: int = 60
    calibration_period_days: int = 7

    # --- Pipeline ---
    nightly_pipeline_time: str = "02:00"  # 2 AM local time
    emotion_baseline_window_days: int = 7
    emotion_anomaly_zscore_threshold: float = 1.5


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
