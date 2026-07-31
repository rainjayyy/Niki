"""Application settings loaded from environment variables and .env file."""

import base64
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)


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

    # --- BE-COMP-006: SMS captcha gating ---
    # When True, /auth/sms/send requires a valid captcha
    # (captcha_id + captcha_answer) and rejects with 400
    # CAPTCHA_INVALID otherwise. Default off so dev / tests
    # don't need a captcha round-trip; flip on in production
    # via NIKI_SMS_CAPTCHA_REQUIRED=true.
    sms_captcha_required: bool = False

    # --- BE-MISC-002: feature flags ---
    # JSON-encoded dict of global flag defaults. Empty `{}` is
    # safe. Example: `{"new_dashboard": true, "silence_alerts":
    # false}`. Per-parent overrides (A/B targeting) are NOT
    # configured here — those will arrive via a future ticket
    # (see BE-MISC-002 followups).
    feature_flags_default: str = "{}"

    # --- BE-AUTH-007: 2FA / TOTP ---
    # Fernet key used to encrypt TOTP secrets at rest. Generated at
    # startup if None; production should set NIKI_TOTP_ENCRYPTION_KEY
    # to a stable urlsafe-base64-32-bytes value so reboots don't
    # invalidate existing rows. When None and no env override is set,
    # TwoFactorService.generate_key() mints one in-process.
    totp_encryption_key: str | None = None
    totp_issuer: str = "Niki"
    # How long a partial_token (2fa_pending scope) stays valid.
    totp_partial_ttl_seconds: int = 5 * 60
    # How long a successful /2fa/verify upgrade-token (full scope)
    # stays valid — used by require_2fa_for_sensitive_ops to decide
    # whether the caller still has a recent re-auth.
    totp_reauth_window_seconds: int = 5 * 60

    # --- BE-API-008: webhook callbacks ---
    # When False (default), POST /webhooks rejects http://
    # targets — only https:// is allowed in production. Set to
    # True for local development against an ngrok / localhost
    # listener via NIKI_ALLOW_INSECURE_WEBHOOK_TARGETS=true.
    allow_insecure_webhook_targets: bool = False

    # --- BE-PUSH-003: Web Push (VAPID, RFC 8292) ---
    # `vapid_private_key` is a PEM-encoded EC (P-256) private key;
    # `vapid_public_key` is the *uncompressed point* (65 bytes,
    # 0x04-prefixed) base64url-encoded — that's the exact shape the
    # browser's `applicationServerKey` expects.
    #
    # Both default to None so dev / test environments don't need any
    # setup: `get_or_generate_vapid_keys()` mints a pair on first use
    # and caches it to disk. Production MUST set them via
    # NIKI_VAPID_PRIVATE_KEY / NIKI_VAPID_PUBLIC_KEY (or ship the
    # cache files) — a regenerated key pair silently invalidates every
    # browser subscription already out in the wild.
    vapid_private_key: str | None = None
    vapid_public_key: str | None = None
    # The `sub` claim of the VAPID JWT: how a push service (FCM /
    # Mozilla / WNS) contacts us when our traffic misbehaves.
    vapid_subject: str = "mailto:admin@niki.app"
    # On-disk cache for an auto-generated pair. Relative paths resolve
    # against the process CWD (repo root in dev).
    vapid_private_key_file: str = "./.vapid_private.pem"
    vapid_public_key_file: str = "./.vapid_public.b64"
    # RFC 8030 delivery hints. TTL = how long the push service holds a
    # message for a currently-offline browser; 24h matches "the parent
    # opens their laptop the next morning and still sees the alert".
    web_push_ttl_seconds: int = 86400
    web_push_urgency: str = "normal"  # very-low | low | normal | high


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()


# ---------------------------------------------------------------------------
# BE-PUSH-003 — VAPID key material
# ---------------------------------------------------------------------------


def _b64url_no_pad(raw: bytes) -> str:
    """base64url-encode without `=` padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _normalize_pem(text: str) -> str:
    """Canonical PEM form: no leading/trailing blank space, one final newline.

    Round-tripping through the disk cache must produce a byte-identical
    string, otherwise `get_or_generate_vapid_keys()` looks like it
    regenerated the key when it didn't.
    """
    return text.strip() + "\n"


def _generate_vapid_pair() -> tuple[str, str]:
    """Mint a fresh (private PEM, public base64url) VAPID pair.

    P-256 is not a preference — RFC 8292 §2 mandates ECDSA on the
    P-256 curve for the application-server key, so there is exactly
    one right answer here.
    """
    # Imported lazily: `cryptography` pulls in a native extension and
    # settings.py is imported by literally everything.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    # The browser wants the raw uncompressed point (0x04 || X || Y),
    # NOT a DER SubjectPublicKeyInfo blob.
    public_point = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _normalize_pem(private_pem), _b64url_no_pad(public_point)


def _read_cached_vapid_pair(
    private_path: Path, public_path: Path
) -> tuple[str, str] | None:
    """Read a previously-generated pair off disk, or None."""
    try:
        if not private_path.is_file() or not public_path.is_file():
            return None
        private_pem = private_path.read_text(encoding="ascii").strip()
        public_b64 = public_path.read_text(encoding="ascii").strip()
    except OSError as exc:  # unreadable / racing writer
        logger.warning("vapid.cache_read_failed", error=str(exc))
        return None
    if not private_pem or not public_b64:
        return None
    return _normalize_pem(private_pem), public_b64


def _write_cached_vapid_pair(
    private_path: Path,
    public_path: Path,
    private_pem: str,
    public_b64: str,
) -> None:
    """Best-effort persist. A read-only FS must not break startup."""
    try:
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_path.write_text(private_pem, encoding="ascii")
        public_path.write_text(public_b64, encoding="ascii")
        # The private key is signing material: owner-read/write only.
        # chmod is a no-op on Windows, which is fine — the dev box
        # isn't the threat model, the container is.
        with suppress(OSError):  # pragma: no cover — platform-dependent
            private_path.chmod(0o600)
    except OSError as exc:
        logger.warning("vapid.cache_write_failed", error=str(exc))


def get_or_generate_vapid_keys(
    settings: "Settings | None" = None,
) -> tuple[str, str]:
    """Return `(private_pem, public_key_b64url)`, generating if needed.

    Resolution order — first hit wins:

      1. `settings.vapid_private_key` + `settings.vapid_public_key`
         (production: injected via env).
      2. The on-disk cache at `vapid_private_key_file` /
         `vapid_public_key_file` (dev: survives a restart).
      3. A freshly generated P-256 pair, written to that cache.

    **Idempotence is the contract.** Two calls in the same process —
    or across a restart — must hand back the same pair, because the
    public key is baked into every browser subscription already
    created. Layers 1 and 2 are what make that true; layer 3 only
    ever runs once.
    """
    settings = settings or get_settings()

    if settings.vapid_private_key and settings.vapid_public_key:
        return settings.vapid_private_key, settings.vapid_public_key

    private_path = Path(settings.vapid_private_key_file)
    public_path = Path(settings.vapid_public_key_file)

    cached = _read_cached_vapid_pair(private_path, public_path)
    if cached is None:
        private_pem, public_b64 = _generate_vapid_pair()
        _write_cached_vapid_pair(
            private_path, public_path, private_pem, public_b64
        )
        logger.info(
            "vapid.keys_generated",
            private_key_file=str(private_path),
            public_key_file=str(public_path),
        )
    else:
        private_pem, public_b64 = cached

    # Cache on the settings singleton so subsequent calls skip the
    # filesystem entirely.
    settings.vapid_private_key = private_pem
    settings.vapid_public_key = public_b64
    return private_pem, public_b64
