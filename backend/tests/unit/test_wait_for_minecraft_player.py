from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "wait_for_minecraft_player.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("wait_for_minecraft_player_script", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
WAIT_SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = WAIT_SCRIPT
SCRIPT_SPEC.loader.exec_module(WAIT_SCRIPT)


def test_parse_online_players_handles_empty_and_multiple_lists() -> None:
    """RCON list parsing preserves exact usernames without status text."""

    assert WAIT_SCRIPT.parse_online_players("unexpected response") == set()
    assert WAIT_SCRIPT.parse_online_players("There are 0 of a max of 20 players online:") == set()
    assert WAIT_SCRIPT.parse_online_players(
        "There are 2 of a max of 20 players online: flysnow_chen, HarnessTrainer1"
    ) == {"flysnow_chen", "HarnessTrainer1"}
