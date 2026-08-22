from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_week11_creative_evaluations"
down_revision = "0002_week10_learning_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the query-friendly MineCLIP creative evaluation table."""

    op.create_table(
        "creative_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False, unique=True),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_threshold", sa.Float(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("scorer", sa.String(length=128), nullable=False, server_default="mineclip"),
        sa.Column("variant", sa.String(length=64), nullable=True),
        sa.Column("calibration_status", sa.String(length=64), nullable=False, server_default="pending"),
        sa.Column("frame_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_creative_evaluations_run_id", "creative_evaluations", ["run_id"])
    op.create_index("ix_creative_evaluations_task_id", "creative_evaluations", ["task_id"])
    op.create_index("ix_creative_evaluations_status", "creative_evaluations", ["status"])


def downgrade() -> None:
    """Remove MineCLIP creative evaluation summaries."""

    op.drop_table("creative_evaluations")
