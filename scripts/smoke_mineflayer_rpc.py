from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Any

from mc_agent_harness.runtime.mineflayer_client import MineflayerClient
from mc_agent_harness.schemas.action import HarnessAction


def parse_args() -> argparse.Namespace:
    """Parse smoke-test options for a live Mineflayer worker."""

    parser = argparse.ArgumentParser(description="Smoke test backend-to-Mineflayer worker RPC.")
    parser.add_argument("--worker-url", default="ws://localhost:8765")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--username", default="HarnessAgent")
    parser.add_argument("--spawn-timeout-ms", type=int, default=15000)
    return parser.parse_args()


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    """Run reset, observe, act, snapshot, and close against a live worker."""

    client = MineflayerClient(args.worker_url, request_timeout=(args.spawn_timeout_ms / 1000) + 5)
    result: dict[str, Any] = {"ok": False}
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
        observation = await client.observe()
        inventory = await client.act(HarnessAction(type="query_inventory", args={}))
        snapshot = await client.snapshot()
        result = {
            "ok": True,
            "observation": observation,
            "inventory": inventory,
            "snapshot": snapshot,
        }
    finally:
        await client.close()
        result["lifecycle_events"] = [asdict(event) for event in client.lifecycle_events]
    return result


def main() -> None:
    """Run the smoke test and print a JSON report."""

    args = parse_args()
    result = asyncio.run(run_smoke(args))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
