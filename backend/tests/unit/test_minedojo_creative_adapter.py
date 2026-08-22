from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mc_agent_harness.tasks.minedojo_creative_adapter import adapt_creative_catalog
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider


ROOT = Path(__file__).resolve().parents[3]
CREATIVE_SOURCE = ROOT / "tasks" / "sources" / "minedojo" / "creative_tasks.yaml"
CREATIVE_MANIFEST = ROOT / "tasks" / "executable" / "minedojo_creative_tasks.jsonl"


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


def test_creative_adapter_builds_external_evaluator_contract() -> None:
    """Creative manifests retain authentic prompts and never expose scores to the agent."""

    source = {
        "creative:test-a": {"prompt": "Build a stone tower", "collection": "manual"},
        "creative:test-b": {"prompt": "Create a flower garden", "collection": "manual"},
        "creative:test-c": {"prompt": "Make a wooden bridge", "collection": "gpt3"},
    }

    manifests, summary = adapt_creative_catalog(source, negative_prompt_count=1)

    manifest = manifests[0]
    assert summary.task_count == 3
    assert summary.collections == {"gpt3": 1, "manual": 2}
    assert manifest["goal"] == "Build a stone tower"
    assert manifest["verifier"]["type"] == "creative_mineclip"
    assert manifest["reset_plan"]["game_mode"] == "survival"
    assert manifest["reset_plan"]["initial_inventory"] == []
    assert manifest["minedojo"]["game_mode_policy"] == (
        "creative_is_task_category_not_minecraft_creative_mode"
    )
    assert manifest["verifier"]["score_threshold"] is None
    assert manifest["verifier"]["calibration"]["status"] == "pending"
    assert manifest["verifier"]["frame_sampling"]["clip_length"] == 16
    assert manifest["minedojo"]["guidance_policy"] == "metadata_only_not_auto_prompted"


def test_creative_adapter_is_deterministic() -> None:
    """Repeated imports select the same contrast prompts and manifest ordering."""

    source = {
        "creative:test-a": {"prompt": "Build a stone tower", "collection": "manual"},
        "creative:test-b": {"prompt": "Create a flower garden", "collection": "manual"},
        "creative:test-c": {"prompt": "Make a wooden bridge", "collection": "gpt3"},
    }

    first, _ = adapt_creative_catalog(source, negative_prompt_count=1)
    second, _ = adapt_creative_catalog(source, negative_prompt_count=1)

    assert first == second


def test_official_creative_source_and_snapshot_cover_all_tasks() -> None:
    """The pinned official YAML and generated executable snapshot both contain 1,560 tasks."""

    source = yaml.safe_load(CREATIVE_SOURCE.read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in CREATIVE_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert isinstance(source, dict)
    assert len(source) == 1560
    assert len(rows) == 1560
    assert len({row["task_id"] for row in rows}) == 1560
    assert {row["minedojo"]["collection"] for row in rows} == {"manual", "youtube", "gpt3"}


@pytest.mark.anyio
async def test_provider_lists_and_loads_creative_snapshot() -> None:
    """The existing TaskProvider reads creative JSONL without a parallel provider API."""

    provider = MineDojoTaskProvider(CREATIVE_MANIFEST)

    summaries = await provider.list_tasks()
    task = await provider.load_task(summaries[0]["task_id"])

    assert len(summaries) == 1560
    assert summaries[0]["creative"] is True
    assert summaries[0]["calibration_status"] == "pending"
    assert task["category"] == "creative"
    assert task["verifier"]["type"] == "creative_mineclip"
