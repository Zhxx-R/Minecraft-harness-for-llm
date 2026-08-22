from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_configuration_center"
down_revision = "0004_week11_human_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add managed knowledge state and prompt-configuration overrides."""

    op.add_column(
        "knowledge_chunks",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "prompt_configurations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("config_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "kind",
            "config_key",
            name="uq_prompt_configurations_kind_key",
        ),
    )
    op.create_index(
        "ix_prompt_configurations_kind",
        "prompt_configurations",
        ["kind"],
    )
    op.create_index(
        "ix_prompt_configurations_config_key",
        "prompt_configurations",
        ["config_key"],
    )


def downgrade() -> None:
    """Remove prompt overrides and managed knowledge state."""

    op.drop_table("prompt_configurations")
    op.drop_column("knowledge_chunks", "version")
    op.drop_column("knowledge_chunks", "enabled")
