from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mc_agent_harness.runtime.mineflayer_client import MineflayerClient
from mc_agent_harness.schemas.action import HarnessAction


ROOT = Path(__file__).resolve().parents[1]


# Default Week 5 crafting chain after wood is gathered through primitive scan/move/dig actions.
CRAFTING_ACTION_PLAN: tuple[dict[str, Any], ...] = (
    {"type": "craft_item", "args": {"item": "oak_planks", "count": 12, "timeout_ms": 10000}},
    {"type": "craft_item", "args": {"item": "crafting_table", "count": 1, "timeout_ms": 10000}},
    {"type": "place_block", "args": {"item": "crafting_table", "timeout_ms": 10000}},
    {"type": "craft_item", "args": {"item": "stick", "count": 4, "timeout_ms": 10000}},
    {
        "type": "craft_item",
        "args": {
            "item": "wooden_pickaxe",
            "count": 1,
            "station": "crafting_table",
            "timeout_ms": 12000,
        },
    },
)


def parse_args() -> argparse.Namespace:
    """Parse options for the live Week 5 action smoke test."""

    parser = argparse.ArgumentParser(description="Smoke test live Week 5 Mineflayer actions.")
    parser.add_argument("--worker-url", default="ws://localhost:8765")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--username", default="Week5Harness")
    parser.add_argument("--spawn-timeout-ms", type=int, default=20000)
    parser.add_argument(
        "--pre-action-delay-sec",
        type=float,
        default=30.0,
        help="Seconds to keep the bot online before actions start, so you can teleport it and place oak_log.",
    )
    parser.add_argument(
        "--action-delay-sec",
        type=float,
        default=0.5,
        help="Seconds to wait between actions for easier visual inspection.",
    )
    parser.add_argument(
        "--hold-open-sec",
        type=float,
        default=5.0,
        help="Seconds to keep the bot online after the action plan completes.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Keep the bot online for one hour after the action plan completes.",
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        default=ROOT / "runs" / "week5_live_actions.json",
        help="JSON report path for the smoke-test audit output.",
    )
    return parser.parse_args()


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    """Connect a bot, wait for manual setup, run Week 5 actions, and return an audit report."""

    client = MineflayerClient(
        args.worker_url,
        request_timeout=max((args.spawn_timeout_ms / 1000) + 5, 30),
    )
    report: dict[str, Any] = {
        "ok": False,
        "host": args.host,
        "port": args.port,
        "username": args.username,
        "pre_action_delay_sec": args.pre_action_delay_sec,
        "steps": [],
    }

    try:
        await client.reset(
            {
                "runtime": {
                    "host": args.host,
                    "port": args.port,
                    "username": args.username,
                    "spawn_timeout_ms": args.spawn_timeout_ms,
                }
            }
        )
        report["initial_observation"] = await client.observe()

        if args.pre_action_delay_sec > 0:
            print(
                (
                    f"Bot {args.username} is online. You have {args.pre_action_delay_sec:g}s "
                    "to teleport it and place oak_log nearby."
                ),
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(args.pre_action_delay_sec)
            report["post_delay_observation"] = await client.observe()

        await _run_action(client, report, {"type": "query_inventory", "args": {}}, args.action_delay_sec)
        scan_result = await _run_action(
            client,
            report,
            {"type": "scan_blocks", "args": {"block": "oak_log", "count": 3, "max_distance": 8}},
            args.action_delay_sec,
        )
        if not scan_result.get("ok") or not scan_result.get("blocks"):
            report["failure"] = scan_result
        else:
            for block in scan_result["blocks"][:3]:
                position = block.get("position")
                if not isinstance(position, dict):
                    continue
                for action in (
                    {"type": "move_to", "args": {"position": position, "tolerance": 2.0, "timeout_ms": 10000}},
                    {"type": "dig_block_at", "args": {"block": "oak_log", "position": position, "timeout_ms": 12000}},
                ):
                    result = await _run_action(client, report, action, args.action_delay_sec)
                    if not result.get("ok"):
                        report["failure"] = result
                        break
                if report.get("failure"):
                    break

                drop_scan = await _run_action(
                    client,
                    report,
                    {"type": "scan_dropped_items", "args": {"item": "oak_log", "count": 1, "max_distance": 8}},
                    args.action_delay_sec,
                )
                drops = drop_scan.get("dropped_items") if isinstance(drop_scan.get("dropped_items"), list) else []
                drop_position = drops[0].get("position") if drops and isinstance(drops[0], dict) else position
                await _run_action(
                    client,
                    report,
                    {"type": "move_to", "args": {"position": drop_position, "tolerance": 0.8, "timeout_ms": 10000}},
                    args.action_delay_sec,
                )
                await _run_action(
                    client,
                    report,
                    {"type": "wait_ticks", "args": {"ticks": 20, "timeout_ms": 3000}},
                    args.action_delay_sec,
                )

            if not report.get("failure"):
                for action in CRAFTING_ACTION_PLAN:
                    result = await _run_action(client, report, action, args.action_delay_sec)
                    if not result.get("ok"):
                        report["failure"] = result
                        break

        report["ok"] = bool(report["steps"]) and all(step["result"].get("ok") is True for step in report["steps"])

        hold_open_sec = 3600.0 if args.keep_open else args.hold_open_sec
        if hold_open_sec > 0:
            print(
                f"Action plan finished. Holding bot online for {hold_open_sec:g}s.",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(hold_open_sec)
    except Exception as error:  # noqa: BLE001 - smoke tests must capture unexpected runtime failures.
        report["exception"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        await client.close()
        report["lifecycle_events"] = [asdict(event) for event in client.lifecycle_events]

    return report


async def _run_action(
    client: MineflayerClient,
    report: dict[str, Any],
    action: dict[str, Any],
    action_delay_sec: float,
) -> dict[str, Any]:
    """Execute one validated worker action and append it to the smoke-test audit report."""

    result = await client.act(HarnessAction(**action))
    report["steps"].append({"action": action, "result": result})
    if action_delay_sec > 0:
        await asyncio.sleep(action_delay_sec)
    return result


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """Build a compact human-readable summary for terminal output."""

    return {
        "ok": report.get("ok"),
        "username": report.get("username"),
        "port": report.get("port"),
        "steps": [
            {
                "action": step["action"]["type"],
                "ok": step["result"].get("ok"),
                "error_code": step["result"].get("error_code"),
                "message": step["result"].get("message"),
            }
            for step in report.get("steps", [])
        ],
        "exception": report.get("exception"),
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Write the full JSON smoke-test report to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    """Run the Week 5 smoke test and print a compact result summary."""

    args = parse_args()
    report = asyncio.run(run_smoke(args))
    write_report(args.audit_output, report)
    summary = summarize(report)
    summary["audit_output"] = str(args.audit_output)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
