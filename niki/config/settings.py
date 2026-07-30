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
    # BE-COMP-003: clients older than this version get 426
    # Upgrade Required. Frontend reports its version via the
    # X-Client-Version header.
    min_supported_version: str = "1.0.0"

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
    # BE-AUTH-001: token TTLs. Access tokens are short-lived (1h); refresh
    # tokens last 30 days and are only used by POST /auth/refresh.
    jwt_access_ttl_seconds: int = 3600
    jwt_refresh_ttl_seconds: int = 30 * 24 * 3600

    # --- Session defaults ---
    default_daily_limit_minutes: int = 60
    calibration_period_days: int = 7

    # --- Pipeline ---
    nightly_pipeline_time: str = "02:00"  # 2 AM local time
    emotion_baseline_window_days: int = 7
    emotion_anomaly_zscore_threshold: float = 1.5

    # --- BE-AUTH-001-rollout-alerts ---
    # Lateral-auth violation burst detection. If the same actor
    # accumulates `auth_alert_burst_count` violations within
    # `auth_alert_window_seconds`, dispatch a SecurityAlert. Set
    # `auth_alert_webhook_url` to fan out to Feishu/Slack/etc.;
    # empty string = log only.
    auth_alert_burst_count: int = 3
    auth_alert_window_seconds: int = 300
    auth_alert_webhook_url: str = ""
    auth_alert_webhook_timeout_s: int = 5

    # --- BE-API-002-redis: shared rate-limit counter backend ---
    # Default keeps current in-memory behaviour for tests / dev.
    # Production: set NIKI_RATE_LIMIT_STORAGE_URI=redis://...
    # When the URI points at Redis, slowapi auto-selects the
    # Redis backend; if Redis is unreachable the limiter falls
    # back to in-memory for the lifetime of the process so
    # requests don't 500 just because the cache is down.
    rate_limit_storage_uri: str = "memory://"

    # --- BE-AUTH-001-rollout-alerts-rollout: email sink ---
    # SMTP delivery of SecurityAlerts. When `auth_alert_smtp_host`
    # is empty the EmailAlertDispatcher short-circuits to a
    # structlog warning, so it's safe to leave blank in dev.
    auth_alert_smtp_host: str = ""
    auth_alert_smtp_port: int = 587
    auth_alert_smtp_username: str = ""
    auth_alert_smtp_password: str = ""
    auth_alert_email_from: str = ""
    auth_alert_email_to: str = ""        # comma-separated recipients
    auth_alert_smtp_timeout_s: int = 5

    # --- BE-MEDIA-001-real: object storage backend ---
    # "local"   → LocalDiskStorage (single-process, dev / PoC)
    # "s3"      → S3CompatibleStorage (Aliyun OSS / Tencent COS /
    #             AWS S3 via boto3, production)
    media_storage_backend: str = "local"
    s3_endpoint_url: str = ""        # empty = AWS S3 default
    s3_region: str = "us-east-1"
    s3_bucket_name: str = "niki-media"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_presign_ttl_seconds: int = 3600
    s3_multipart_threshold_bytes: int = 5 * 1024 * 1024  # 5 MB
    s3_addressing_style: str = "auto"  # "auto" | "path" | "virtual"


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
