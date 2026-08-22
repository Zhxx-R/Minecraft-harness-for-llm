from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any

from mc_agent_harness.runtime.server_commands import RconServerCommandExecutor


def parse_args() -> argparse.Namespace:
    """Parse the RCON-backed visible-client readiness options."""

    parser = argparse.ArgumentParser(description="Wait for one Minecraft player to be online.")
    parser.add_argument("--host", default=os.getenv("MINECRAFT_RCON_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MINECRAFT_RCON_PORT", "25575")),
    )
    parser.add_argument("--password", default=os.getenv("MINECRAFT_RCON_PASSWORD"))
    parser.add_argument("--player", required=True)
    parser.add_argument("--timeout-sec", type=float, default=300.0)
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)
    return parser.parse_args()


def parse_online_players(response: str) -> set[str]:
    """Extract exact player names from Minecraft's RCON `/list` response."""

    _status, separator, player_segment = response.rpartition(":")
    if not separator:
        return set()
    return {value.strip() for value in player_segment.split(",") if value.strip()}


async def wait_for_player(
    executor: Any,
    player: str,
    *,
    timeout_sec: float,
    poll_interval_sec: float,
) -> set[str]:
    """Poll authenticated server state until the named client appears or time expires."""

    if timeout_sec <= 0 or poll_interval_sec <= 0:
        raise ValueError("timeout_sec and poll_interval_sec must be positive.")
    deadline = time.monotonic() + timeout_sec
    last_error = "player was not listed"
    while time.monotonic() < deadline:
        result = (await executor.execute_many(["/list"]))[0]
        if result.ok and result.response:
            online_players = parse_online_players(result.response)
            if player in online_players:
                return online_players
            last_error = f"online players: {sorted(online_players)}"
        else:
            last_error = result.error or result.response or "RCON /list failed"
        await asyncio.sleep(poll_interval_sec)
    raise TimeoutError(
        f"Minecraft player {player!r} did not join within {timeout_sec:.0f} seconds "
        f"({last_error})."
    )


async def run(args: argparse.Namespace) -> set[str]:
    """Validate configuration and wait through the concrete RCON executor."""

    if not args.password:
        raise ValueError("MINECRAFT_RCON_PASSWORD or --password is required.")
    executor = RconServerCommandExecutor(
        host=args.host,
        port=args.port,
        password=args.password,
        timeout_sec=5,
    )
    return await wait_for_player(
        executor,
        args.player,
        timeout_sec=args.timeout_sec,
        poll_interval_sec=args.poll_interval_sec,
    )


def main() -> None:
    """Block the local workflow until the first-person spectator client is ready."""

    args = parse_args()
    try:
        online_players = asyncio.run(run(args))
    except (OSError, TimeoutError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Spectator player {args.player} is online; players={sorted(online_players)}")


if __name__ == "__main__":
    main()
