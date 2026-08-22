import json
from pathlib import Path

import pytest

from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider
from mc_agent_harness.training import (
    InMemoryTrainingQueue,
    TrainingBudget,
    TrainingJobConfig,
    TrainingRunner,
    write_training_report,
)


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_DIR = ROOT / "tasks" / "manifests"


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_week9_training_runs_tasks_in_parallel_with_isolated_namespaces() -> None:
    """Validate parallel scheduling, queue state, and per-task memory isolation."""

    provider = MineDojoTaskProvider(MANIFEST_DIR)
    config = TrainingJobConfig(
        job_id="week9_test_parallel",
        budget=TrainingBudget(worker_concurrency=5),
    )
    queue = InMemoryTrainingQueue()
    runner = TrainingRunner(provider, config, queue)

    report = await runner.run(
        task_ids=[
            "minedojo_harvest_oak_log",
            "minedojo_techtree_oak_planks",
            "minedojo_techtree_crafting_table",
            "minedojo_techtree_stick",
            "minedojo_techtree_wooden_pickaxe",
        ]
    )

    namespaces = {outcome.memory_namespace for outcome in report.outcomes}
    assert report.task_count == 5
    assert report.success_count == 5
    assert report.success_rate == 1.0
    assert report.max_observed_concurrency == 5
    assert len(namespaces) == 5
    assert all(namespace.startswith("week9_test_parallel:") for namespace in namespaces)
    assert {state.status for state in report.queue_states} == {"succeeded"}


@pytest.mark.anyio
async def test_week9_training_reports_step_budget_failures(tmp_path: Path) -> None:
    """Validate that per-task max step budgets are enforced through the runner config."""

    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    (manifest_dir / "two_step_task.json").write_text(
        json.dumps(
            {
                "task_id": "week9_two_step_task",
                "source": "test",
                "category": "techtree",
                "family": "TechTree",
                "goal": "Craft planks, then a crafting table.",
                "allowed_actions": ["craft_item", "query_inventory"],
                "verifier": {"type": "inventory_contains", "item": "crafting_table", "count": 1},
                "success_criteria": {
                    "type": "inventory_contains",
                    "item": "crafting_table",
                    "count": 1,
                },
                "benchmark": {
                    "max_steps": 2,
                    "initial_state": {
                        "inventory": [{"name": "oak_log", "count": 1}],
                        "nearby_blocks": [],
                        "nearby_entities": [],
                    },
                    "scripted_actions": [
                        {"type": "craft_item", "args": {"item": "oak_planks", "count": 4}},
                        {"type": "craft_item", "args": {"item": "crafting_table", "count": 1}},
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    provider = MineDojoTaskProvider(manifest_dir)
    config = TrainingJobConfig(
        job_id="week9_test_budget",
        budget=TrainingBudget(max_steps_per_task=1, worker_concurrency=2),
    )
    runner = TrainingRunner(provider, config)

    report = await runner.run(task_ids=["week9_two_step_task"])

    assert report.task_count == 1
    assert report.success_count == 0
    assert report.status == "completed_with_failures"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].steps == 1


@pytest.mark.anyio
async def test_week9_training_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    """Validate report artifacts are written for audit and interview demos."""

    provider = MineDojoTaskProvider(MANIFEST_DIR)
    config = TrainingJobConfig(
        job_id="week9_test_report",
        budget=TrainingBudget(worker_concurrency=2),
    )
    runner = TrainingRunner(provider, config)
    report = await runner.run(task_ids=["minedojo_harvest_oak_log", "minedojo_harvest_dirt"])

    json_path, markdown_path = write_training_report(report, tmp_path)

    assert json_path.exists()
    assert markdown_path.exists()
    assert "week9_test_report" in markdown_path.read_text(encoding="utf-8")
    assert "minedojo_harvest_oak_log" in markdown_path.read_text(encoding="utf-8")
