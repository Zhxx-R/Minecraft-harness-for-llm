from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.harness.context_manager import ContextManager  # noqa: E402
from mc_agent_harness.harness.tool_registry import DEFAULT_HARNESS_ACTIONS  # noqa: E402
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse CLI options for prompt inspection."""

    parser = argparse.ArgumentParser(description="Dump the exact prompt messages for one task step.")
    parser.add_argument("--task-id", required=True, help="Task manifest id, for example minedojo_harvest_oak_log.")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "tasks" / "manifests")
    parser.add_argument(
        "--observation-json",
        default=None,
        help="Optional JSON object overriding the benchmark initial observation.",
    )
    parser.add_argument(
        "--use-manifest-action-scope",
        action="store_true",
        help="Inspect the historical manifest action list instead of the live full harness scope.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print system/user message content.")
    return parser.parse_args()


def main() -> None:
    """Run the async prompt builder and print JSON to stdout."""

    args = parse_args()
    payload = asyncio.run(_build_prompt_dump(args))
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


async def _build_prompt_dump(args: argparse.Namespace) -> dict[str, Any]:
    """Build prompt messages from a local task manifest."""

    provider = MineDojoTaskProvider(args.manifest_dir)
    task_spec = await provider.load_task(args.task_id)
    manifest_allowed_actions = list(task_spec.get("allowed_actions", []))
    allowed_actions = (
        manifest_allowed_actions
        if args.use_manifest_action_scope
        else list(DEFAULT_HARNESS_ACTIONS)
    )
    prompt_task_spec = {
        **task_spec,
        "allowed_actions": allowed_actions,
        "manifest_allowed_actions": manifest_allowed_actions,
    }
    observation = (
        json.loads(args.observation_json)
        if args.observation_json
        else _observation_from_task_spec(task_spec)
    )
    context = await ContextManager().build(
        observation=observation,
        task_memory=[],
        task_spec=prompt_task_spec,
        allowed_actions=allowed_actions,
    )
    messages = context.messages
    if args.pretty:
        messages = [_pretty_message(message) for message in messages]
    return {
        "task_id": args.task_id,
        "allowed_actions": allowed_actions,
        "manifest_allowed_actions": manifest_allowed_actions,
        "messages": messages,
        "prompt_sections": context.prompt_sections,
        "resolved_terms": [term.canonical_id for term in context.resolved_terms],
        "retrieved_docs": [document.id for document in context.retrieved_docs],
        "retrieved_skills": [skill.name for skill in context.retrieved_skills],
    }


def _observation_from_task_spec(task_spec: dict[str, Any]) -> dict[str, Any]:
    """Create a first-step observation from benchmark initial_state metadata."""

    benchmark = task_spec.get("benchmark") if isinstance(task_spec.get("benchmark"), dict) else {}
    initial_state = benchmark.get("initial_state") if isinstance(benchmark.get("initial_state"), dict) else {}
    return {
        "position": initial_state.get("position", {"x": 0, "y": 65, "z": 0}),
        "health": 20,
        "food": 20,
        "inventory": initial_state.get("inventory", []),
        "nearby_blocks": initial_state.get("nearby_blocks", []),
        "nearby_entities": initial_state.get("nearby_entities", []),
    }


def _pretty_message(message: dict[str, Any]) -> dict[str, Any]:
    """Parse JSON message content where possible for easier terminal inspection."""

    content = message.get("content")
    if isinstance(content, str):
        try:
            return {**message, "content": json.loads(content)}
        except json.JSONDecodeError:
            return message
    return message


if __name__ == "__main__":
    main()
