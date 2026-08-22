from pathlib import Path

import pytest

from mc_agent_harness.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkRunner,
    ScriptedBenchmarkRuntime,
    write_benchmark_report,
)
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_DIR = ROOT / "tasks" / "manifests"


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_week6_benchmark_runs_curated_task_set() -> None:
    provider = MineDojoTaskProvider(MANIFEST_DIR)
    runner = BenchmarkRunner(provider, BenchmarkConfig())

    report = await runner.run()

    assert report.task_count == 10
    assert report.success_count == 10
    assert report.success_rate == 1.0
    assert report.runtime_crash_rate == 0.0
    assert report.invalid_action_rate == 0.0
    assert report.total_steps == 34


@pytest.mark.anyio
async def test_week6_benchmark_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    provider = MineDojoTaskProvider(MANIFEST_DIR)
    runner = BenchmarkRunner(provider, BenchmarkConfig())
    report = await runner.run(task_ids=["minedojo_techtree_wooden_pickaxe"])

    json_path, markdown_path = write_benchmark_report(report, tmp_path)

    assert json_path.exists()
    assert markdown_path.exists()
    assert "minedojo_techtree_wooden_pickaxe" in markdown_path.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_scripted_runtime_requires_explicit_equip_before_ranged_combat() -> None:
    """Scripted combat should match live worker semantics: gear choice belongs to the agent."""

    runtime = ScriptedBenchmarkRuntime()
    await runtime.reset(
        {
            "task_id": "combat_phantom_test",
            "benchmark": {
                "initial_state": {
                    "inventory": [{"name": "bow", "count": 1}, {"name": "arrow", "count": 16}],
                    "nearby_entities": [
                        {
                            "id": 12,
                            "name": "phantom",
                            "type": "mob",
                            "position": {"x": 4, "y": 70, "z": 2},
                            "target_airborne": True,
                        }
                    ],
                }
            },
        }
    )

    failed = await runtime.act(HarnessAction(type="engage_combat", args={"entity": "phantom", "mode": "ranged"}))
    equipped = await runtime.act(HarnessAction(type="equip_item", args={"item": "bow", "slot": "hand"}))
    succeeded = await runtime.act(HarnessAction(type="engage_combat", args={"entity": "phantom", "mode": "ranged"}))

    assert failed["ok"] is False
    assert failed["status"] == "weapon_not_equipped"
    assert failed["suggested_next_actions"] == ["equip_item", "query_inventory"]
    assert equipped["ok"] is True
    assert equipped["equipment_after"]["main_hand"] == {"name": "bow", "count": 1}
    assert succeeded["ok"] is True
    assert succeeded["status"] == "target_killed"


@pytest.mark.anyio
async def test_scripted_follow_persists_through_observe_and_stops_on_next_action() -> None:
    """The deterministic runtime mirrors live cross-turn follow lifecycle semantics."""

    runtime = ScriptedBenchmarkRuntime()
    await runtime.reset(
        {
            "task_id": "follow_sheep_test",
            "benchmark": {
                "initial_state": {
                    "nearby_entities": [
                        {
                            "id": 143,
                            "name": "sheep",
                            "type": "animal",
                            "position": {"x": 8, "y": 65, "z": 2},
                        }
                    ],
                }
            },
        }
    )

    started = await runtime.act(
        HarnessAction(type="follow", args={"entity_id": 143, "follow_distance": 1.25})
    )
    observed = await runtime.observe()
    next_result = await runtime.act(HarnessAction(type="query_inventory", args={}))
    observed_after = await runtime.observe()

    assert started["active_follow"]["target"]["id"] == 143
    assert started["recommended_next_actions"][0].startswith("use_item:")
    assert started["recommended_next_actions"][1].startswith(
        "move_to_and_engage_combat:"
    )
    assert observed["active_follow"]["until"] == "next_action_received"
    assert next_result["persistent_follow_stopped"]["stop_reason"] == "next_action_received"
    assert observed_after["active_follow"] is None
