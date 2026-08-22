from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0006_run_round_tracing"
down_revision = "0005_configuration_center"
branch_labels = None
depends_on = None

_TRACE_DOMAIN = "mc-agent-harness.trace.v1"
_ROOT_SPAN_DOMAIN = "mc-agent-harness.run-root-span.v1"
_ROUND_SPAN_DOMAIN = "mc-agent-harness.round-span.v1"
_BATCH_SIZE = 2_000


def upgrade() -> None:
    """Add durable run/round tracing and backfill every existing audit record."""

    op.add_column("runs", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.add_column("runs", sa.Column("root_span_id", sa.String(length=16), nullable=True))

    op.add_column("steps", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.add_column("steps", sa.Column("span_id", sa.String(length=16), nullable=True))

    op.add_column("trajectory_events", sa.Column("step_index", sa.Integer(), nullable=True))
    op.add_column(
        "trajectory_events",
        sa.Column("trace_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "trajectory_events",
        sa.Column("span_id", sa.String(length=16), nullable=True),
    )

    op.add_column("model_calls", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.add_column("model_calls", sa.Column("span_id", sa.String(length=16), nullable=True))

    op.add_column(
        "runtime_errors",
        sa.Column("trace_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "runtime_errors",
        sa.Column("span_id", sa.String(length=16), nullable=True),
    )

    op.create_table(
        "round_spans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("runs.id"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("span_id", sa.String(length=16), nullable=False),
        sa.Column("parent_span_id", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
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
        sa.UniqueConstraint("run_id", "step_index", name="uq_round_spans_run_step"),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_round_spans_trace_span"),
    )

    _backfill_trace_context()
    _require_complete_backfill()
    _make_trace_columns_required()
    _create_trace_indexes()


def downgrade() -> None:
    """Remove tracing projections while leaving the original audit payloads intact."""

    op.drop_index("ix_runtime_errors_span_id", table_name="runtime_errors")
    op.drop_index("ix_runtime_errors_trace_id", table_name="runtime_errors")
    op.drop_index("ix_model_calls_span_id", table_name="model_calls")
    op.drop_index("ix_model_calls_trace_id", table_name="model_calls")
    op.drop_index("ix_trajectory_events_span_id", table_name="trajectory_events")
    op.drop_index("ix_trajectory_events_trace_id", table_name="trajectory_events")
    op.drop_index("ix_trajectory_events_step_index", table_name="trajectory_events")
    op.drop_index("ix_steps_span_id", table_name="steps")
    op.drop_index("ix_steps_trace_id", table_name="steps")
    op.drop_index("ix_round_spans_status", table_name="round_spans")
    op.drop_index("ix_round_spans_parent_span_id", table_name="round_spans")
    op.drop_index("ix_round_spans_span_id", table_name="round_spans")
    op.drop_index("ix_round_spans_trace_id", table_name="round_spans")
    op.drop_index("ix_round_spans_run_id", table_name="round_spans")
    op.drop_index("ix_runs_root_span_id", table_name="runs")
    op.drop_index("ix_runs_trace_id", table_name="runs")

    op.drop_table("round_spans")
    op.drop_column("runtime_errors", "span_id")
    op.drop_column("runtime_errors", "trace_id")
    op.drop_column("model_calls", "span_id")
    op.drop_column("model_calls", "trace_id")
    op.drop_column("trajectory_events", "span_id")
    op.drop_column("trajectory_events", "trace_id")
    op.drop_column("trajectory_events", "step_index")
    op.drop_column("steps", "span_id")
    op.drop_column("steps", "trace_id")
    op.drop_column("runs", "root_span_id")
    op.drop_column("runs", "trace_id")


def _backfill_trace_context() -> None:
    """Backfill run roots, round spans, and every persisted child log."""

    connection = op.get_bind()
    tables = _trace_tables()
    run_rows = list(
        connection.execute(
            sa.select(
                tables["runs"].c.id,
                tables["runs"].c.status,
                tables["runs"].c.started_at,
                tables["runs"].c.finished_at,
            )
        ).mappings()
    )
    trace_by_run = {str(row["id"]): _trace_id(str(row["id"])) for row in run_rows}
    root_span_by_run = {str(row["id"]): _root_span_id(str(row["id"])) for row in run_rows}
    run_metadata = {str(row["id"]): row for row in run_rows}

    _execute_batches(
        connection,
        sa.update(tables["runs"])
        .where(tables["runs"].c.id == sa.bindparam("_run_id"))
        .values(
            trace_id=sa.bindparam("_trace_id"),
            root_span_id=sa.bindparam("_root_span_id"),
        ),
        [
            {
                "_run_id": run_id,
                "_trace_id": trace_by_run[run_id],
                "_root_span_id": root_span_by_run[run_id],
            }
            for run_id in trace_by_run
        ],
    )

    rounds: dict[tuple[str, int], dict[str, Any]] = {}
    _backfill_events(connection, tables, trace_by_run, root_span_by_run, rounds)
    _backfill_steps(connection, tables, trace_by_run, rounds)
    _backfill_model_calls(connection, tables, trace_by_run, rounds)
    _backfill_runtime_errors(
        connection,
        tables,
        trace_by_run,
        root_span_by_run,
        rounds,
    )

    round_rows: list[dict[str, Any]] = []
    for (run_id, step_index), aggregate in sorted(rounds.items()):
        run = run_metadata[run_id]
        status = str(aggregate["status"])
        if status == "active" and str(run["status"]) != "running":
            status = "incomplete"
        started_at = aggregate["started_at"] or run["started_at"]
        finished_at = (
            None
            if status == "active"
            else aggregate["last_at"] or run["finished_at"] or started_at
        )
        round_rows.append(
            {
                "run_id": run_id,
                "step_index": step_index,
                "trace_id": trace_by_run[run_id],
                "span_id": _round_span_id(run_id, step_index),
                "parent_span_id": root_span_by_run[run_id],
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "attributes": {
                    "backfilled": True,
                    "event_count": aggregate["event_count"],
                    "model_call_count": aggregate["model_call_count"],
                    "runtime_error_count": aggregate["runtime_error_count"],
                    "has_step_record": aggregate["has_step_record"],
                },
            }
        )
    _execute_insert_batches(connection, tables["round_spans"], round_rows)


def _backfill_events(
    connection: Any,
    tables: dict[str, sa.TableClause],
    trace_by_run: dict[str, str],
    root_span_by_run: dict[str, str],
    rounds: dict[tuple[str, int], dict[str, Any]],
) -> None:
    rows = connection.execute(
        sa.select(
            tables["events"].c.id,
            tables["events"].c.run_id,
            tables["events"].c.event_type,
            tables["events"].c.payload,
            tables["events"].c.created_at,
            tables["events"].c.updated_at,
        )
    ).mappings()
    updates: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        payload = _payload_dict(row["payload"])
        step_index = _step_index(payload.get("step_index"))
        span_id = (
            _round_span_id(run_id, step_index)
            if step_index is not None
            else root_span_by_run[run_id]
        )
        payload = {**payload, "trace_id": trace_by_run[run_id], "span_id": span_id}
        updates.append(
            {
                "_row_id": row["id"],
                "_step_index": step_index,
                "_trace_id": trace_by_run[run_id],
                "_span_id": span_id,
                "_payload": payload,
            }
        )
        if step_index is not None:
            status = _event_terminal_status(str(row["event_type"]), payload)
            _touch_round(
                rounds,
                run_id,
                step_index,
                row["created_at"],
                row["updated_at"],
                status=status,
                event_count=1,
            )
    _execute_batches(
        connection,
        sa.update(tables["events"])
        .where(tables["events"].c.id == sa.bindparam("_row_id"))
        .values(
            step_index=sa.bindparam("_step_index"),
            trace_id=sa.bindparam("_trace_id"),
            span_id=sa.bindparam("_span_id"),
            payload=sa.bindparam("_payload"),
        ),
        updates,
    )


def _backfill_steps(
    connection: Any,
    tables: dict[str, sa.TableClause],
    trace_by_run: dict[str, str],
    rounds: dict[tuple[str, int], dict[str, Any]],
) -> None:
    rows = connection.execute(
        sa.select(
            tables["steps"].c.id,
            tables["steps"].c.run_id,
            tables["steps"].c.step_index,
            tables["steps"].c.action_result,
            tables["steps"].c.created_at,
            tables["steps"].c.updated_at,
        )
    ).mappings()
    updates: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        step_index = int(row["step_index"])
        updates.append(
            {
                "_row_id": row["id"],
                "_trace_id": trace_by_run[run_id],
                "_span_id": _round_span_id(run_id, step_index),
            }
        )
        _touch_round(
            rounds,
            run_id,
            step_index,
            row["created_at"],
            row["updated_at"],
            status=_result_status(_payload_dict(row["action_result"])),
            has_step_record=True,
        )
    _execute_batches(
        connection,
        sa.update(tables["steps"])
        .where(tables["steps"].c.id == sa.bindparam("_row_id"))
        .values(
            trace_id=sa.bindparam("_trace_id"),
            span_id=sa.bindparam("_span_id"),
        ),
        updates,
    )


def _backfill_model_calls(
    connection: Any,
    tables: dict[str, sa.TableClause],
    trace_by_run: dict[str, str],
    rounds: dict[tuple[str, int], dict[str, Any]],
) -> None:
    rows = connection.execute(
        sa.select(
            tables["calls"].c.id,
            tables["calls"].c.run_id,
            tables["calls"].c.step_index,
            tables["calls"].c.created_at,
            tables["calls"].c.updated_at,
        )
    ).mappings()
    updates: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        step_index = int(row["step_index"])
        updates.append(
            {
                "_row_id": row["id"],
                "_trace_id": trace_by_run[run_id],
                "_span_id": _round_span_id(run_id, step_index),
            }
        )
        _touch_round(
            rounds,
            run_id,
            step_index,
            row["created_at"],
            row["updated_at"],
            model_call_count=1,
        )
    _execute_batches(
        connection,
        sa.update(tables["calls"])
        .where(tables["calls"].c.id == sa.bindparam("_row_id"))
        .values(
            trace_id=sa.bindparam("_trace_id"),
            span_id=sa.bindparam("_span_id"),
        ),
        updates,
    )


def _backfill_runtime_errors(
    connection: Any,
    tables: dict[str, sa.TableClause],
    trace_by_run: dict[str, str],
    root_span_by_run: dict[str, str],
    rounds: dict[tuple[str, int], dict[str, Any]],
) -> None:
    rows = connection.execute(
        sa.select(
            tables["errors"].c.id,
            tables["errors"].c.run_id,
            tables["errors"].c.step_index,
            tables["errors"].c.payload,
            tables["errors"].c.created_at,
            tables["errors"].c.updated_at,
        )
    ).mappings()
    updates: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        step_index = _step_index(row["step_index"])
        payload = _payload_dict(row["payload"])
        if step_index is not None:
            payload["step_index"] = step_index
        span_id = (
            _round_span_id(run_id, step_index)
            if step_index is not None
            else root_span_by_run[run_id]
        )
        payload = {**payload, "trace_id": trace_by_run[run_id], "span_id": span_id}
        updates.append(
            {
                "_row_id": row["id"],
                "_trace_id": trace_by_run[run_id],
                "_span_id": span_id,
                "_payload": payload,
            }
        )
        if step_index is not None:
            _touch_round(
                rounds,
                run_id,
                step_index,
                row["created_at"],
                row["updated_at"],
                status="error",
                runtime_error_count=1,
            )
    _execute_batches(
        connection,
        sa.update(tables["errors"])
        .where(tables["errors"].c.id == sa.bindparam("_row_id"))
        .values(
            trace_id=sa.bindparam("_trace_id"),
            span_id=sa.bindparam("_span_id"),
            payload=sa.bindparam("_payload"),
        ),
        updates,
    )


def _touch_round(
    rounds: dict[tuple[str, int], dict[str, Any]],
    run_id: str,
    step_index: int,
    created_at: datetime | None,
    updated_at: datetime | None,
    *,
    status: str | None = None,
    event_count: int = 0,
    model_call_count: int = 0,
    runtime_error_count: int = 0,
    has_step_record: bool = False,
) -> None:
    aggregate = rounds.setdefault(
        (run_id, step_index),
        {
            "status": "active",
            "started_at": created_at,
            "last_at": updated_at or created_at,
            "event_count": 0,
            "model_call_count": 0,
            "runtime_error_count": 0,
            "has_step_record": False,
        },
    )
    if created_at is not None and (
        aggregate["started_at"] is None or created_at < aggregate["started_at"]
    ):
        aggregate["started_at"] = created_at
    latest = updated_at or created_at
    if latest is not None and (
        aggregate["last_at"] is None or latest > aggregate["last_at"]
    ):
        aggregate["last_at"] = latest
    if status is not None and _status_rank(status) >= _status_rank(str(aggregate["status"])):
        aggregate["status"] = status
    aggregate["event_count"] += event_count
    aggregate["model_call_count"] += model_call_count
    aggregate["runtime_error_count"] += runtime_error_count
    aggregate["has_step_record"] = aggregate["has_step_record"] or has_step_record


def _event_terminal_status(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "action_result":
        return _result_status(_payload_dict(payload.get("result")))
    if event_type in {"runtime_error", "runtime_action_timeout"}:
        return "error"
    return None


def _result_status(result: dict[str, Any]) -> str:
    return "error" if result.get("ok") is False else "ok"


def _status_rank(status: str) -> int:
    return {"active": 0, "incomplete": 1, "ok": 2, "error": 3}.get(status, 0)


def _require_complete_backfill() -> None:
    connection = op.get_bind()
    tables = _trace_tables()
    checks = [
        ("runs.trace_id", tables["runs"], tables["runs"].c.trace_id),
        ("runs.root_span_id", tables["runs"], tables["runs"].c.root_span_id),
        ("steps.trace_id", tables["steps"], tables["steps"].c.trace_id),
        ("steps.span_id", tables["steps"], tables["steps"].c.span_id),
        ("trajectory_events.trace_id", tables["events"], tables["events"].c.trace_id),
        ("trajectory_events.span_id", tables["events"], tables["events"].c.span_id),
        ("model_calls.trace_id", tables["calls"], tables["calls"].c.trace_id),
        ("model_calls.span_id", tables["calls"], tables["calls"].c.span_id),
        ("runtime_errors.trace_id", tables["errors"], tables["errors"].c.trace_id),
        ("runtime_errors.span_id", tables["errors"], tables["errors"].c.span_id),
    ]
    incomplete = []
    for label, table, column in checks:
        count = connection.scalar(sa.select(sa.func.count()).select_from(table).where(column.is_(None)))
        if count:
            incomplete.append(f"{label}={count}")
    if incomplete:
        raise RuntimeError("Trace backfill left NULL values: " + ", ".join(incomplete))


def _make_trace_columns_required() -> None:
    columns = {
        "runs": (("trace_id", sa.String(length=32)), ("root_span_id", sa.String(length=16))),
        "steps": (("trace_id", sa.String(length=32)), ("span_id", sa.String(length=16))),
        "trajectory_events": (
            ("trace_id", sa.String(length=32)),
            ("span_id", sa.String(length=16)),
        ),
        "model_calls": (("trace_id", sa.String(length=32)), ("span_id", sa.String(length=16))),
        "runtime_errors": (
            ("trace_id", sa.String(length=32)),
            ("span_id", sa.String(length=16)),
        ),
    }
    if op.get_bind().dialect.name == "sqlite":
        for table_name, table_columns in columns.items():
            with op.batch_alter_table(table_name) as batch:
                for column_name, column_type in table_columns:
                    batch.alter_column(
                        column_name,
                        existing_type=column_type,
                        nullable=False,
                    )
        return
    for table_name, table_columns in columns.items():
        for column_name, column_type in table_columns:
            op.alter_column(
                table_name,
                column_name,
                existing_type=column_type,
                nullable=False,
            )


def _create_trace_indexes() -> None:
    op.create_index("ix_runs_trace_id", "runs", ["trace_id"], unique=True)
    op.create_index("ix_runs_root_span_id", "runs", ["root_span_id"], unique=True)
    op.create_index("ix_round_spans_run_id", "round_spans", ["run_id"])
    op.create_index("ix_round_spans_trace_id", "round_spans", ["trace_id"])
    op.create_index("ix_round_spans_span_id", "round_spans", ["span_id"])
    op.create_index("ix_round_spans_parent_span_id", "round_spans", ["parent_span_id"])
    op.create_index("ix_round_spans_status", "round_spans", ["status"])
    op.create_index("ix_steps_trace_id", "steps", ["trace_id"])
    op.create_index("ix_steps_span_id", "steps", ["span_id"])
    op.create_index("ix_trajectory_events_step_index", "trajectory_events", ["step_index"])
    op.create_index("ix_trajectory_events_trace_id", "trajectory_events", ["trace_id"])
    op.create_index("ix_trajectory_events_span_id", "trajectory_events", ["span_id"])
    op.create_index("ix_model_calls_trace_id", "model_calls", ["trace_id"])
    op.create_index("ix_model_calls_span_id", "model_calls", ["span_id"])
    op.create_index("ix_runtime_errors_trace_id", "runtime_errors", ["trace_id"])
    op.create_index("ix_runtime_errors_span_id", "runtime_errors", ["span_id"])


def _trace_tables() -> dict[str, sa.TableClause]:
    return {
        "runs": sa.table(
            "runs",
            sa.column("id", sa.String()),
            sa.column("status", sa.String()),
            sa.column("trace_id", sa.String()),
            sa.column("root_span_id", sa.String()),
            sa.column("started_at", sa.DateTime(timezone=True)),
            sa.column("finished_at", sa.DateTime(timezone=True)),
        ),
        "round_spans": sa.table(
            "round_spans",
            sa.column("run_id", sa.String()),
            sa.column("step_index", sa.Integer()),
            sa.column("trace_id", sa.String()),
            sa.column("span_id", sa.String()),
            sa.column("parent_span_id", sa.String()),
            sa.column("status", sa.String()),
            sa.column("started_at", sa.DateTime(timezone=True)),
            sa.column("finished_at", sa.DateTime(timezone=True)),
            sa.column("attributes", sa.JSON()),
        ),
        "steps": sa.table(
            "steps",
            sa.column("id", sa.Integer()),
            sa.column("run_id", sa.String()),
            sa.column("step_index", sa.Integer()),
            sa.column("trace_id", sa.String()),
            sa.column("span_id", sa.String()),
            sa.column("action_result", sa.JSON()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        "events": sa.table(
            "trajectory_events",
            sa.column("id", sa.Integer()),
            sa.column("run_id", sa.String()),
            sa.column("event_type", sa.String()),
            sa.column("payload", sa.JSON()),
            sa.column("step_index", sa.Integer()),
            sa.column("trace_id", sa.String()),
            sa.column("span_id", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        "calls": sa.table(
            "model_calls",
            sa.column("id", sa.Integer()),
            sa.column("run_id", sa.String()),
            sa.column("step_index", sa.Integer()),
            sa.column("trace_id", sa.String()),
            sa.column("span_id", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        "errors": sa.table(
            "runtime_errors",
            sa.column("id", sa.Integer()),
            sa.column("run_id", sa.String()),
            sa.column("step_index", sa.Integer()),
            sa.column("trace_id", sa.String()),
            sa.column("span_id", sa.String()),
            sa.column("payload", sa.JSON()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
    }


def _execute_batches(connection: Any, statement: Any, values: list[dict[str, Any]]) -> None:
    for batch in _batches(values):
        connection.execute(statement, batch)


def _execute_insert_batches(
    connection: Any,
    table: sa.TableClause,
    values: list[dict[str, Any]],
) -> None:
    for batch in _batches(values):
        connection.execute(sa.insert(table), batch)


def _batches(values: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(values), _BATCH_SIZE):
        yield values[offset : offset + _BATCH_SIZE]


def _payload_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _step_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _trace_id(run_id: str) -> str:
    return _stable_hex(_TRACE_DOMAIN, run_id, length=32)


def _root_span_id(run_id: str) -> str:
    return _stable_hex(_ROOT_SPAN_DOMAIN, run_id, length=16)


def _round_span_id(run_id: str, step_index: int) -> str:
    return _stable_hex(_ROUND_SPAN_DOMAIN, run_id, str(step_index), length=16)


def _stable_hex(domain: str, *parts: str, length: int) -> str:
    digest = sha256("\0".join((domain, *parts)).encode("utf-8")).hexdigest()[:length]
    if set(digest) == {"0"}:
        return f"{'0' * (length - 1)}1"
    return digest
