"""tenants (API clients), reversals, outbox

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14 10:00:00

Existing rows (from before multi-tenancy) are adopted by a "legacy" client so nothing is lost.
Its API key is a random value nobody knows; create a real client with POST /admin/clients and,
if you need the old data reachable, re-point it with an UPDATE.
"""
import hashlib
import secrets
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )

    op.add_column("accounts", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_accounts_client_id"), "accounts", ["client_id"], unique=False)
    op.create_foreign_key("fk_accounts_client_id", "accounts", "api_clients", ["client_id"], ["id"])

    op.add_column("transactions", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.add_column("transactions", sa.Column("reversal_of_id", sa.Uuid(), nullable=True))

    # adopt pre-existing data
    bind = op.get_bind()
    has_data = bind.execute(sa.text("SELECT 1 FROM transactions LIMIT 1")).first() or \
        bind.execute(sa.text("SELECT 1 FROM accounts WHERE kind = 'USER' LIMIT 1")).first()
    if has_data:
        legacy = uuid.uuid4()
        bind.execute(sa.text(
            "INSERT INTO api_clients (id, name, key_hash, is_active) VALUES (:id, 'legacy', :h, true)"),
            {"id": legacy, "h": hashlib.sha256(secrets.token_bytes(32)).hexdigest()})
        bind.execute(sa.text("UPDATE accounts SET client_id = :c WHERE kind = 'USER'"), {"c": legacy})
        bind.execute(sa.text("UPDATE transactions SET client_id = :c"), {"c": legacy})

    with op.batch_alter_table("transactions") as batch:
        batch.alter_column("client_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_constraint("transactions_idempotency_key_key", type_="unique")
        batch.create_unique_constraint("uq_transactions_client_key", ["client_id", "idempotency_key"])
        batch.create_unique_constraint("uq_transactions_reversal_of_id", ["reversal_of_id"])
        batch.create_foreign_key("fk_transactions_client_id", "api_clients", ["client_id"], ["id"])
        batch.create_foreign_key("fk_transactions_reversal_of_id", "transactions", ["reversal_of_id"], ["id"])
    op.create_index(op.f("ix_transactions_client_id"), "transactions", ["client_id"], unique=False)

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "DELIVERED", "DEAD", name="outboxstatus",
                                    native_enum=False, length=10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_status_next", "outbox_events", ["status", "next_attempt_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_outbox_status_next", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(op.f("ix_transactions_client_id"), table_name="transactions")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("fk_transactions_reversal_of_id", type_="foreignkey")
        batch.drop_constraint("fk_transactions_client_id", type_="foreignkey")
        batch.drop_constraint("uq_transactions_reversal_of_id", type_="unique")
        batch.drop_constraint("uq_transactions_client_key", type_="unique")
        batch.create_unique_constraint("transactions_idempotency_key_key", ["idempotency_key"])
    op.drop_column("transactions", "reversal_of_id")
    op.drop_column("transactions", "client_id")
    op.drop_constraint("fk_accounts_client_id", "accounts", type_="foreignkey")
    op.drop_index(op.f("ix_accounts_client_id"), table_name="accounts")
    op.drop_column("accounts", "client_id")
    op.drop_table("api_clients")
