from __future__ import annotations

import json
import random
from pathlib import Path

from mc_agent_harness.launcher.catalog import ExecutableTaskCatalog


def test_catalog_filters_paginates_and_returns_safe_detail(tmp_path: Path) -> None:
    """The launch catalog keeps snapshots server-side while exposing useful task metadata."""

    programmatic_path, creative_path = _write_catalog(tmp_path)
    catalog = ExecutableTaskCatalog(programmatic_path, creative_path)

    tasks, total, categories, kinds = catalog.list_tasks(
        query="oak",
        kind="programmatic",
        offset=0,
        limit=10,
    )

    assert total == 1
    assert tasks[0].task_id == "harvest_oak_log"
    assert kinds == {"programmatic": 2, "creative": 1}
    assert categories == {"harvest": 1, "combat": 1, "creative": 1}
    detail = tasks[0].detail()
    assert detail["verifier_type"] == "inventory_delta"
    assert detail["biome_hint"] == "forest"
    assert detail["allowed_actions"] == ["scan_blocks", "move_to"]
    assert "manifest_path" not in detail
    all_tasks, _, _, _ = catalog.list_tasks(limit=10)
    assert all_tasks[0].kind == "programmatic"


def test_catalog_random_selection_uses_current_filters(tmp_path: Path) -> None:
    """Random draw cannot escape the user's active kind and category filters."""

    programmatic_path, creative_path = _write_catalog(tmp_path)
    catalog = ExecutableTaskCatalog(programmatic_path, creative_path)

    selected = catalog.random_task(
        kind="creative",
        category="creative",
        random_source=random.Random(7),
    )

    assert selected.task_id == "creative:1"
    assert selected.kind == "creative"


def _write_catalog(tmp_path: Path) -> tuple[Path, Path]:
    """Create minimal trusted JSONL snapshots for catalog tests."""

    programmatic_path = tmp_path / "programmatic.jsonl"
    creative_path = tmp_path / "creative.jsonl"
    programmatic = [
        {
            "task_id": "harvest_oak_log",
            "category": "harvest",
            "family": "Harvest",
            "goal": "Collect one oak log",
            "reset_plan": {"biome_hint": "forest", "initial_inventory": []},
            "verifier": {"type": "inventory_delta"},
            "allowed_actions": ["scan_blocks", "move_to"],
        },
        {
            "task_id": "combat_zombie",
            "category": "combat",
            "family": "Combat",
            "goal": "Defeat one zombie",
            "reset_plan": {"spawn_mobs": [{"entity": "zombie", "count": 1}]},
            "verifier": {"type": "kill_stat_delta"},
        },
    ]
    creative = [
        {
            "task_id": "creative:1",
            "category": "creative",
            "family": "Creative",
            "goal": "Build a small shelter",
            "verifier": {"type": "human_review"},
        }
    ]
    programmatic_path.write_text(
        "\n".join(json.dumps(task) for task in programmatic) + "\n",
        encoding="utf-8",
    )
    creative_path.write_text(
        "\n".join(json.dumps(task) for task in creative) + "\n",
        encoding="utf-8",
    )
    return programmatic_path, creative_path
