from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Accept the connection string exactly as a host (Neon, Render, Heroku...) hands it out.

    Hosts give "postgres://..." or "postgresql://...?sslmode=require&channel_binding=require",
    but our async driver (asyncpg) wants the "postgresql+asyncpg://" scheme, spells the TLS
    option "ssl", and does not know "channel_binding". Translating here means you can paste
    the string as-is. Strings that are already right (and non-Postgres URLs) pass through.
    """
    parts = urlsplit(url)
    if parts.scheme in ("postgres", "postgresql"):
        parts = parts._replace(scheme="postgresql+asyncpg")
    if parts.scheme != "postgresql+asyncpg":
        return url
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        query.append(("ssl" if key == "sslmode" else key, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


class Settings(BaseSettings):
    # e.g. postgresql+asyncpg://user:pass@localhost:5432/payment_ledger
    # (a plain postgres:// or postgresql:// string from your host also works)
    database_url: str
    sql_echo: bool = False  # set SQL_ECHO=true to print every SQL statement while debugging
    # Operator key for /admin/* and /metrics. Unset = those endpoints answer 503 (fail closed).
    admin_api_key: str | None = None
    # requests per minute allowed per API client (token bucket); 0 turns the limiter off
    rate_limit_per_minute: int = 120
    # outbox -> webhook delivery. No URL = events are still recorded, just not delivered.
    webhook_url: str | None = None
    webhook_secret: str = ""
    webhook_timeout_seconds: float = 5.0
    outbox_max_attempts: int = 8
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_database_url(value)


settings = Settings()
