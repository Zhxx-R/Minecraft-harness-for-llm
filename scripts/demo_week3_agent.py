from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mc_agent_harness.core.config import settings
from mc_agent_harness.db.session import SessionLocal
from mc_agent_harness.harness.context_manager import ContextManager
from mc_agent_harness.harness.database_state_store import DatabaseStateStore
from mc_agent_harness.harness.evaluation import EvaluationRecorder
from mc_agent_harness.harness.execution_loop import ExecutionBudget, ExecutionLoop
from mc_agent_harness.harness.persistent_recorder import PersistentEvaluationRecorder
from mc_agent_harness.harness.tool_registry import ToolRegistry
from mc_agent_harness.knowledge.chunk_store import DatabaseKnowledgeStore
from mc_agent_harness.knowledge.database_provider import DatabaseKnowledgeProvider
from mc_agent_harness.knowledge.static_provider import StaticKnowledgeProvider
from mc_agent_harness.models.router import ModelRouter
from mc_agent_harness.runtime.mineflayer_client import MineflayerClient


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse options for a live Week 3 single-agent demo."""

    parser = argparse.ArgumentParser(
        description="Run a live Week 3 observe-context-model-action demo."
    )
    parser.add_argument(
        "--task",
        choices=["inventory", "mine-log"],
        default="inventory",
        help="Small controlled task to run through the Week 3 execution loop.",
    )
    parser.add_argument("--worker-url", default=settings.mineflayer_worker_url)
    parser.add_argument("--host", default=os.getenv("MINECRAFT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MINECRAFT_PORT", "25565")))
    parser.add_argument("--username", default=os.getenv("MINECRAFT_USERNAME", "HarnessAgent"))
    parser.add_argument(
        "--spawn-timeout-ms",
        type=int,
        default=int(os.getenv("MINECRAFT_SPAWN_TIMEOUT_MS", "15000")),
    )
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument(
        "--persist-db",
        action="store_true",
        help="Persist trajectory, model calls, checkpoints, and knowledge retrieval metadata to SQL.",
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        default=None,
        help="Optional audit JSON path. Defaults to runs/week3_demo_<timestamp>.json.",
    )
    return parser.parse_args()


async def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    """Run one live model-backed Week 3 task and return a JSON-safe report."""

    _validate_model_environment()
    task_spec = _task_spec(args)
    recorder: EvaluationRecorder
    context_manager: ContextManager | None = None
    state_store: DatabaseStateStore | None = None
    seeded_knowledge_chunks: int | None = None
    if args.persist_db:
        recorder = PersistentEvaluationRecorder(SessionLocal)
        state_store = DatabaseStateStore(SessionLocal)
        knowledge_store = DatabaseKnowledgeStore(SessionLocal)
        seeded_knowledge_chunks = knowledge_store.upsert_static_provider(StaticKnowledgeProvider())
        context_manager = ContextManager(
            knowledge_provider=DatabaseKnowledgeProvider(knowledge_store)
        )
    else:
        recorder = EvaluationRecorder()
    runtime = MineflayerClient(
        args.worker_url,
        request_timeout=(args.spawn_timeout_ms / 1000) + 5,
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(),
        context_manager=context_manager,
        tool_registry=ToolRegistry(task_spec["allowed_actions"]),
        recorder=recorder,
        state_store=state_store,
        budget=ExecutionBudget(max_steps=args.max_steps, checkpoint_interval_steps=1),
    )

    try:
        result = await loop.run(
            task_spec["task_id"],
            task_spec=task_spec,
            task_memory=_task_memory(args.task),
        )
    finally:
        await runtime.close()

    return {
        "ok": True,
        "model": settings.model_default,
        "task": args.task,
        "persistence": {
            "database": args.persist_db,
            "seeded_knowledge_chunks": seeded_knowledge_chunks,
        },
        "run": {
            "run_id": result.run_id,
            "task_id": result.task_id,
            "terminated": result.terminated,
            "steps": [
                {
                    "step_index": step.step_index,
                    "action": step.action.model_dump(),
                    "action_result": step.action_result,
                }
                for step in result.steps
            ],
        },
        "worker_lifecycle_events": [asdict(event) for event in runtime.lifecycle_events],
        "trajectory_events": [asdict(event) for event in recorder.events],
    }


def _validate_model_environment() -> None:
    """Fail fast when the Qwen-compatible model environment is incomplete."""

    if not settings.qwen_base_url:
        raise SystemExit("QWEN_BASE_URL is missing. Check your local .env file.")
    if not settings.qwen_api_key:
        raise SystemExit("QWEN_API_KEY is missing. Check your local .env file.")


def _task_spec(args: argparse.Namespace) -> dict[str, Any]:
    """Build the task manifest fragment used by the live demo."""

    runtime = {
        "host": args.host,
        "port": args.port,
        "username": args.username,
        "spawn_timeout_ms": args.spawn_timeout_ms,
    }
    if args.task == "inventory":
        return {
            "task_id": "week3_inventory_query",
            "goal": "Check the current Minecraft inventory. Return the query_inventory action.",
            "runtime": runtime,
            "allowed_actions": ["query_inventory"],
        }

    return {
        "task_id": "week3_dig_nearby_log",
        "goal": "Find one nearby oak_log block, move close enough, dig its explicit coordinate, then wait for pickup.",
        "runtime": runtime,
        "allowed_actions": ["query_inventory", "scan_blocks", "move_to", "dig_block_at", "scan_dropped_items", "wait_ticks"],
    }


def _task_memory(task: str) -> list[str]:
    """Provide tiny task-local memory hints without leaking extra Minecraft strategy."""

    if task == "inventory":
        return ["The only useful action for this task is query_inventory with empty args."]
    return [
        "Use scan_blocks to find oak_log coordinates, move_to a reachable coordinate near the block, dig_block_at the block position, then wait_ticks after moving to any dropped oak_log."
    ]


def _default_audit_path() -> Path:
    """Return a timestamped audit path under the ignored runs directory."""

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "runs" / f"week3_demo_{timestamp}.json"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Write a JSON demo report to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    """Run the live Week 3 demo and print a short result summary."""

    args = parse_args()
    report = asyncio.run(run_demo(args))
    audit_path = args.audit_output or _default_audit_path()
    _write_report(audit_path, report)

    steps = report["run"]["steps"]
    action = steps[0]["action"] if steps else None
    action_result = steps[0]["action_result"] if steps else None
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "model": report["model"],
                "task": report["task"],
                "run_id": report["run"]["run_id"],
                "action": action,
                "action_result": action_result,
                "audit_output": str(audit_path),
                "persist_db": report["persistence"]["database"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
