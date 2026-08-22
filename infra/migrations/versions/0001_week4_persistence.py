from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_week4_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create Week 4 persistence tables and enable pgvector on Postgres."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("task_spec", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resumed_from_checkpoint_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_runs_task_id", "runs", ["task_id"])
    op.create_index("ix_runs_status", "runs", ["status"])

    op.create_table(
        "steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("observation", sa.JSON(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("action_result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "step_index", name="uq_steps_run_step"),
    )
    op.create_index("ix_steps_run_id", "steps", ["run_id"])

    op.create_table(
        "trajectory_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_trajectory_events_run_id", "trajectory_events", ["run_id"])
    op.create_index("ix_trajectory_events_event_type", "trajectory_events", ["event_type"])
    op.create_index("ix_trajectory_events_task_id", "trajectory_events", ["task_id"])
    op.create_index("ix_trajectory_events_agent_id", "trajectory_events", ["agent_id"])

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("raw_response", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_calls_run_id", "model_calls", ["run_id"])

    op.create_table(
        "runtime_errors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_runtime_errors_run_id", "runtime_errors", ["run_id"])

    op.create_table(
        "task_memories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("namespace", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("memory_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_memories_task_id", "task_memories", ["task_id"])
    op.create_index("ix_task_memories_namespace", "task_memories", ["namespace"])

    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("source_run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_skills_name_version"),
    )
    op.create_index("ix_skills_name", "skills", ["name"])
    op.create_index("ix_skills_status", "skills", ["status"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("chunk_metadata", sa.JSON(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_source", "knowledge_chunks", ["source"])

    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_checkpoints_run_id", "checkpoints", ["run_id"])
    op.create_foreign_key(
        "fk_runs_resumed_from_checkpoint_id",
        "runs",
        "checkpoints",
        ["resumed_from_checkpoint_id"],
        ["id"],
    )


def downgrade() -> None:
    """Drop Week 4 persistence tables."""

    op.drop_constraint("fk_runs_resumed_from_checkpoint_id", "runs", type_="foreignkey")
    op.drop_table("checkpoints")
    op.drop_table("knowledge_chunks")
    op.drop_table("skills")
    op.drop_table("task_memories")
    op.drop_table("runtime_errors")
    op.drop_table("model_calls")
    op.drop_table("trajectory_events")
    op.drop_table("steps")
    op.drop_table("runs")
