from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Application ─────────────────────────────────────────────────────
    APP_NAME: str = "Weft API"
    APP_VERSION: str = "1.0.0"
    ENV: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/v1"

    # ── Database ────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://weft:weft_dev@localhost:5432/weft_dev"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://weft:weft_dev@localhost:5432/weft_test"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10

    # ── Redis ───────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── JWT / Auth ──────────────────────────────────────────────────────
    JWT_PRIVATE_KEY: str = ""
    JWT_PUBLIC_KEY: str = ""
    JWT_ALGORITHM: str = "RS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── OAuth ───────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    APPLE_CLIENT_ID: str = ""

    # ── AWS ──────────────────────────────────────────────────────────────
    AWS_REGION: str = "ap-south-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_BUCKET_NAME: str = "weft-issues-dev"
    CLOUDFRONT_DOMAIN: str = ""

    # ── Firebase / FCM ──────────────────────────────────────────────────
    FCM_SERVICE_ACCOUNT_JSON: str = ""
    FCM_PROJECT_ID: str = ""

    # ── Media storage ───────────────────────────────────────────────────
    # Local-disk object storage stands in for S3 until AWS credentials exist.
    # `app/core/storage.py` implements the same interface both ways, so moving
    # to S3 is a constructor swap, not a rewrite of the image pipeline.
    MEDIA_ROOT: str = "./var/media"
    MEDIA_BASE_URL: str = "/media"
    MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    MAX_IMAGES_PER_ISSUE: int = 5
    IMAGE_MAX_DIMENSION: int = 1920  # long edge, px — resized down, never up

    # ── reCAPTCHA ───────────────────────────────────────────────────────
    RECAPTCHA_SECRET_KEY: str = ""
    RECAPTCHA_SCORE_THRESHOLD: float = 0.5

    # ── CORS ────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://weft.city",
        "https://admin.weft.city",
    ]


settings = Settings()
