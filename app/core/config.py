from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Document Processing & Compliance Management Platform"
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/docplatform"

    SECRET_KEY: str = "dev-only-secret-change-me-dev-only-secret"
    BCRYPT_ROUNDS: int = 12
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    SUPER_ADMIN_EMAIL: str | None = None

    STORAGE_DIR: str = "./storage"
    MAX_UPLOAD_SIZE_MB: int = 10
    ALLOWED_CONTENT_TYPES: list[str] = [
        "application/pdf",
        "image/png",
        "image/jpeg",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]

    SCHEDULER_ENABLED: bool = False
    SCHEDULER_INTERVAL_SECONDS: int = 300
    JOB_MAX_ATTEMPTS: int = 3
    EXPIRY_NOTICE_DAYS: int = 30
    REVIEW_REMINDER_HOURS: int = 24
    REVIEW_ESCALATION_HOURS: int = 72

    EMAIL_ENABLED: bool = False
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 25
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = "no-reply@docplatform.local"

    AUTO_CREATE_TABLES: bool = False

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
