from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_week10_learning_candidates"
down_revision = "0001_week4_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the evidence-gated failure-learning candidate table."""

    op.create_table(
        "learning_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("signature", sa.String(length=512), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("failure_status", sa.String(length=128), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column("support_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contradiction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.2"),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("knowledge_refs", sa.JSON(), nullable=False),
        sa.Column("source_run_ids", sa.JSON(), nullable=False),
        sa.Column("recovery_run_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_learning_candidates_signature", "learning_candidates", ["signature"])
    op.create_index("ix_learning_candidates_scope_key", "learning_candidates", ["scope_key"])
    op.create_index("ix_learning_candidates_kind", "learning_candidates", ["kind"])
    op.create_index("ix_learning_candidates_status", "learning_candidates", ["status"])
    op.create_index("ix_learning_candidates_target", "learning_candidates", ["target"])


def downgrade() -> None:
    """Remove failure-learning candidate persistence."""

    op.drop_table("learning_candidates")
