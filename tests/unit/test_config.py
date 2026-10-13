import pytest

from app.core.config import normalize_database_url


@pytest.mark.parametrize("given, expected", [
    # what Neon / Render / Heroku hand out
    ("postgresql://u:p@host/db?sslmode=require&channel_binding=require", "postgresql+asyncpg://u:p@host/db?ssl=require"),
    ("postgres://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ("postgresql://u:p@host:5432/db?sslmode=require", "postgresql+asyncpg://u:p@host:5432/db?ssl=require"),
    # already correct: untouched
    ("postgresql+asyncpg://u:p@host/db?ssl=require", "postgresql+asyncpg://u:p@host/db?ssl=require"),
    ("postgresql+asyncpg://ledger_user:ledger_password@localhost:5432/payment_ledger",
     "postgresql+asyncpg://ledger_user:ledger_password@localhost:5432/payment_ledger"),
    # other databases are not our business
    ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
])
def test_database_urls_are_normalised(given, expected):
    assert normalize_database_url(given) == expected


def test_other_query_options_survive():
    out = normalize_database_url("postgresql://u:p@h/db?sslmode=require&application_name=ledger")
    assert "ssl=require" in out and "application_name=ledger" in out and "sslmode" not in out
