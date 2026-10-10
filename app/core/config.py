from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # e.g. postgresql+asyncpg://user:pass@localhost:5432/payment_ledger
    database_url: str
    sql_echo: bool = False  # set SQL_ECHO=true to print every SQL statement while debugging

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
