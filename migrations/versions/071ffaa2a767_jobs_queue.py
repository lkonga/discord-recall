"""jobs queue

Revision ID: 071ffaa2a767
Revises: 15f8f8f0640c
Create Date: 2026-09-24 00:19:29.740625

Only the jobs table is created here: autogenerate also wanted to re-add the
existing foreign keys, which SQLite cannot ALTER (they are already declared in
the initial migration and enforced by the table definitions).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "071ffaa2a767"
down_revision: Union[str, None] = "15f8f8f0640c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("phase", sa.String(length=60), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("server_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_jobs_status_created", "jobs", ["status", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_table("jobs")
