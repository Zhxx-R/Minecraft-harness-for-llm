from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_week11_human_reviews"
down_revision = "0003_week11_creative_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the authoritative human-review queue for creative-task evidence."""

    op.create_table(
        "human_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("runs.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("task_name", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.String(length=64),
            nullable=False,
            server_default="awaiting_review",
        ),
        sa.Column("submission_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("reviewer_id", sa.String(length=255), nullable=True),
        sa.Column("decision", sa.String(length=64), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_human_reviews_run_id", "human_reviews", ["run_id"])
    op.create_index("ix_human_reviews_task_id", "human_reviews", ["task_id"])
    op.create_index("ix_human_reviews_status", "human_reviews", ["status"])
    op.create_index("ix_human_reviews_reviewer_id", "human_reviews", ["reviewer_id"])
    op.create_index("ix_human_reviews_decision", "human_reviews", ["decision"])
    _backfill_existing_creative_evaluations()


def downgrade() -> None:
    """Remove the human-review queue while leaving creative score evidence intact."""

    op.drop_table("human_reviews")


def _backfill_existing_creative_evaluations() -> None:
    """Place pre-migration creative evidence into the new pending review queue."""

    evaluations = sa.table(
        "creative_evaluations",
        sa.column("run_id", sa.String()),
        sa.column("task_id", sa.String()),
        sa.column("prompt", sa.Text()),
        sa.column("result", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    reviews = sa.table(
        "human_reviews",
        sa.column("run_id", sa.String()),
        sa.column("task_id", sa.String()),
        sa.column("task_name", sa.String()),
        sa.column("status", sa.String()),
        sa.column("submission_summary", sa.Text()),
        sa.column("evidence", sa.JSON()),
        sa.column("reason_codes", sa.JSON()),
        sa.column("notes", sa.Text()),
        sa.column("submitted_at", sa.DateTime(timezone=True)),
        sa.column("version", sa.Integer()),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.select(
                evaluations.c.run_id,
                evaluations.c.task_id,
                evaluations.c.prompt,
                evaluations.c.result,
                evaluations.c.created_at,
            )
        ).mappings()
    )
    for row in rows:
        result = dict(row["result"] or {})
        connection.execute(
            sa.insert(reviews).values(
                run_id=row["run_id"],
                task_id=row["task_id"],
                task_name=row["prompt"] or row["task_id"],
                status="awaiting_review",
                submission_summary="",
                evidence={
                    "source": result.get("evidence_source"),
                    "final_frame": result.get("final_frame"),
                    "key_frames": result.get("key_frames") or [],
                    "mineclip": {
                        "status": "inconclusive"
                        if result.get("inconclusive")
                        else "completed",
                        "score": result.get("score"),
                        "score_threshold": result.get("score_threshold"),
                        "calibration": result.get("calibration"),
                        "window_count": result.get("window_count"),
                        "frame_count": result.get("frame_count"),
                        "reason": result.get("reason"),
                    },
                },
                reason_codes=[],
                notes="",
                submitted_at=row["created_at"],
                version=1,
            )
        )
