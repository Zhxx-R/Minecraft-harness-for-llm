from pathlib import Path

import pytest

from mc_agent_harness.tasks.catalog import (
    MineDojoCatalogSource,
    MineDojoProgrammaticCatalog,
    build_programmatic_catalog,
    flatten_suite_tasks,
)
from mc_agent_harness.tasks.similarity import (
    DiverseBatchPlanner,
    DiverseWavePlanner,
    TaskDescriptor,
    TaskSimilarityScorer,
)


ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = ROOT / "tasks" / "catalog" / "minedojo_programmatic_tasks.jsonl"


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_week10_catalog_loads_full_programmatic_task_snapshot() -> None:
    """Validate the local MineDojo programmatic catalog snapshot."""

    catalog = MineDojoProgrammaticCatalog(CATALOG_PATH)

    tasks = await catalog.list_tasks()
    loaded = await catalog.load_task("harvest_1_log")

    assert len(tasks) == 1581
    assert loaded["task_id"] == "harvest_1_log"
    assert loaded["category"] == "harvest"
    assert loaded["success_criteria"]["type"] == "minedojo_programmatic"


def test_week10_catalog_builder_tracks_suite_tiers_and_categories() -> None:
    """Validate catalog normalization from MineDojo-style YAML payloads."""

    source = MineDojoCatalogSource(programmatic_tasks_url="memory://programmatic_tasks.yaml")
    records, summary = build_programmatic_catalog(
        {
            "harvest_1_log": {
                "category": "harvest",
                "prompt": "obtain log",
                "guidance": "find a tree",
            },
            "combat_zombie_plains_barehand": {"category": "combat", "prompt": "combat a zombie"},
        },
        source=source,
        suite_tasks=flatten_suite_tasks({"programmatic_tasks": {"standard": ["harvest_1_log"]}}),
    )

    assert summary.task_count == 2
    assert summary.categories == {"combat": 1, "harvest": 1}
    assert summary.suite_tiers == {"not_in_official_suite": 1, "standard": 1}
    assert records[0].success_criteria["catalog_only"] is True


def test_week10_similarity_planner_avoids_near_duplicate_parallel_tasks() -> None:
    """Validate that greedy batch selection avoids highly similar task pairs first."""

    tasks = [
        {
            "task_id": "harvest_1_log",
            "category": "harvest",
            "family": "Harvest",
            "goal": "obtain log",
            "allowed_actions": ["dig_block_at"],
            "knowledge_tags": ["minecraft:term/log"],
            "success_criteria": {"type": "inventory_contains", "item": "log"},
        },
        {
            "task_id": "harvest_8_log",
            "category": "harvest",
            "family": "Harvest",
            "goal": "obtain 8 log",
            "allowed_actions": ["dig_block_at"],
            "knowledge_tags": ["minecraft:term/log"],
            "success_criteria": {"type": "inventory_contains", "item": "log"},
        },
        {
            "task_id": "combat_zombie_plains_barehand",
            "category": "combat",
            "family": "Combat",
            "goal": "combat a zombie in plains",
            "allowed_actions": ["fight_entity"],
            "knowledge_tags": ["minecraft:term/zombie"],
            "success_criteria": {"type": "entity_defeated", "entity": "zombie"},
        },
    ]
    planner = DiverseBatchPlanner()

    selection = planner.select_batch(tasks, batch_size=2, max_pairwise_similarity=0.45)

    assert "harvest_1_log" in selection.selected_task_ids
    assert "combat_zombie_plains_barehand" in selection.selected_task_ids
    assert "harvest_8_log" in selection.deferred_task_ids
    assert selection.max_pairwise_similarity < 0.45


def test_week10_task_similarity_scores_near_duplicates_higher() -> None:
    """Validate task similarity components rank same-target tasks above unrelated tasks."""

    scorer = TaskSimilarityScorer()
    harvest_log = TaskDescriptor.from_task_spec(
        {
            "task_id": "harvest_1_log",
            "category": "harvest",
            "goal": "obtain log",
            "allowed_actions": ["dig_block_at"],
            "success_criteria": {"type": "inventory_contains", "item": "log"},
        }
    )
    harvest_log_many = TaskDescriptor.from_task_spec(
        {
            "task_id": "harvest_8_log",
            "category": "harvest",
            "goal": "obtain 8 log",
            "allowed_actions": ["dig_block_at"],
            "success_criteria": {"type": "inventory_contains", "item": "log"},
        }
    )
    combat_zombie = TaskDescriptor.from_task_spec(
        {
            "task_id": "combat_zombie_plains_barehand",
            "category": "combat",
            "goal": "combat zombie",
            "allowed_actions": ["fight_entity"],
            "success_criteria": {"type": "entity_defeated", "entity": "zombie"},
        }
    )

    assert scorer.score(harvest_log, harvest_log_many).total > scorer.score(
        harvest_log,
        combat_zombie,
    ).total


def test_week10_wave_planner_separates_near_duplicate_tasks() -> None:
    """Two-server waves should pair each harvest variant with an unrelated task when possible."""

    tasks = [
        {
            "task_id": "harvest_1_log",
            "category": "harvest",
            "goal": "obtain log",
            "knowledge_tags": ["log"],
            "success_criteria": {"type": "inventory_contains", "item": "log"},
        },
        {
            "task_id": "harvest_8_log",
            "category": "harvest",
            "goal": "obtain 8 log",
            "knowledge_tags": ["log"],
            "success_criteria": {"type": "inventory_contains", "item": "log"},
        },
        {
            "task_id": "combat_zombie",
            "category": "combat",
            "goal": "defeat zombie",
            "knowledge_tags": ["zombie"],
            "success_criteria": {"type": "entity_defeated", "entity": "zombie"},
        },
        {
            "task_id": "techtree_compass",
            "category": "techtree",
            "goal": "use compass",
            "knowledge_tags": ["compass"],
            "success_criteria": {"type": "item_used", "item": "compass"},
        },
    ]

    plan = DiverseWavePlanner().arrange(
        tasks,
        wave_size=2,
        max_pairwise_similarity=0.45,
    )

    assert len(plan.waves) == 2
    assert sorted(task_id for wave in plan.waves for task_id in wave) == sorted(
        task["task_id"] for task in tasks
    )
    assert not any(
        {"harvest_1_log", "harvest_8_log"}.issubset(set(wave))
        for wave in plan.waves
    )
    assert plan.threshold_violations == []
