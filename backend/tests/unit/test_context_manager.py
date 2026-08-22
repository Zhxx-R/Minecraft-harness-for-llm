import json
from pathlib import Path

import pytest

from mc_agent_harness.configuration.defaults import (
    ACTION_PROMPT_KIND,
    HOT_RELOAD_KEY,
    RUNTIME_SETTING_KIND,
    SYSTEM_PROMPT_KEY,
    SYSTEM_PROMPT_KIND,
    default_action_payload,
)
from mc_agent_harness.configuration.service import PromptConfigEntry, PromptConfigSnapshot
from mc_agent_harness.harness.context_manager import (
    ContextBuildResult,
    ContextManager,
    ContextPolicy,
)
from mc_agent_harness.harness.context_memory import RunContextMemory
from mc_agent_harness.schemas.action import HarnessAction, MemoryUpdate
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus
from mc_agent_harness.skills.library import SkillLibrarySnapshot


class RotatingPromptConfigProvider:
    """Return one immutable prompt revision per ContextManager build."""

    def __init__(self, snapshots: list[PromptConfigSnapshot]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def snapshot(self) -> PromptConfigSnapshot:
        snapshot = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return snapshot


class RecoveringPromptConfigProvider:
    """Fail once, then expose a valid prompt snapshot."""

    def __init__(self, snapshot: PromptConfigSnapshot) -> None:
        self.configured_snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> PromptConfigSnapshot:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary database outage")
        return self.configured_snapshot


def _prompt_snapshot(
    *,
    version: int,
    system_prompt: str,
    follow_visible: bool,
    follow_recommendations: list[str],
    hot_reload_enabled: bool = True,
) -> PromptConfigSnapshot:
    """Build a small configured snapshot for hot-update tests."""

    follow_payload = default_action_payload("follow")
    follow_payload.update(
        {
            "purpose": f"configured follow purpose v{version}",
            "prompt_visible": follow_visible,
            "recommended_next_actions": follow_recommendations,
        }
    )
    inventory_payload = default_action_payload("query_inventory")
    inventory_payload.update(
        {
            "prompt_visible": not follow_visible,
            "recommended_next_actions": [f"scan_blocks: revision {version}"],
        }
    )
    return PromptConfigSnapshot(
        system=PromptConfigEntry(
            kind=SYSTEM_PROMPT_KIND,
            config_key=SYSTEM_PROMPT_KEY,
            display_name="Agent system prompt",
            enabled=True,
            payload={"content": system_prompt},
            version=version,
            persisted=True,
        ),
        actions={
            "follow": PromptConfigEntry(
                kind=ACTION_PROMPT_KIND,
                config_key="follow",
                display_name="follow",
                enabled=True,
                payload=follow_payload,
                version=version,
                persisted=True,
            ),
            "query_inventory": PromptConfigEntry(
                kind=ACTION_PROMPT_KIND,
                config_key="query_inventory",
                display_name="query_inventory",
                enabled=True,
                payload=inventory_payload,
                version=version,
                persisted=True,
            ),
        },
        hot_reload=PromptConfigEntry(
            kind=RUNTIME_SETTING_KIND,
            config_key=HOT_RELOAD_KEY,
            display_name="Prompt hot reload",
            enabled=True,
            payload={"enabled": hot_reload_enabled},
            version=version,
            persisted=True,
        ),
    )


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_context_manager_hot_loads_one_atomic_prompt_snapshot_per_turn() -> None:
    first_snapshot = _prompt_snapshot(
        version=1,
        system_prompt="configured system prompt v1",
        follow_visible=True,
        follow_recommendations=["use_item: configured v1"],
    )
    second_snapshot = _prompt_snapshot(
        version=2,
        system_prompt="configured system prompt v2",
        follow_visible=False,
        follow_recommendations=["use_item: configured v2"],
    )
    provider = RotatingPromptConfigProvider([first_snapshot, second_snapshot])
    manager = ContextManager(prompt_config_provider=provider)  # type: ignore[arg-type]
    build_kwargs = {
        "observation": {"health": 20, "inventory": []},
        "task_memory": [],
        "task_spec": {"goal": "Inspect or follow a target."},
        "allowed_actions": ["follow", "query_inventory"],
    }

    first = await manager.build(**build_kwargs)
    second = await manager.build(**build_kwargs)

    assert provider.calls == 2
    assert first.messages[0]["content"].startswith("configured system prompt v1")
    assert second.messages[0]["content"].startswith("configured system prompt v2")
    assert first.prompt_visible_actions == ["follow"]
    assert second.prompt_visible_actions == ["query_inventory"]
    assert first.prompt_sections["stable_system_payload"]["action_contract"][
        "allowed_actions"
    ] == ["follow"]
    assert first.prompt_sections["stable_system_payload"]["available_action_primitives"][
        0
    ]["purpose"] == "configured follow purpose v1"
    assert first.action_recommendations["follow"] == ["use_item: configured v1"]
    assert second.action_recommendations["follow"] == ["use_item: configured v2"]
    assert (
        first.prompt_sections["prompt_configuration"]["revision"]
        != second.prompt_sections["prompt_configuration"]["revision"]
    )
    assert first.prompt_sections["prompt_configuration"]["hot_reload_enabled"] is True
    assert first.prompt_sections["prompt_configuration"]["snapshot_mode"] == "live"
    assert second.prompt_sections["prompt_configuration"]["snapshot_mode"] == "live"


@pytest.mark.anyio
async def test_context_manager_pins_prompt_snapshot_for_run_when_hot_reload_is_off() -> None:
    pinned_snapshot = _prompt_snapshot(
        version=1,
        system_prompt="configured pinned system prompt",
        follow_visible=True,
        follow_recommendations=["use_item: pinned revision"],
        hot_reload_enabled=False,
    )
    unread_snapshot = _prompt_snapshot(
        version=2,
        system_prompt="configured system prompt that must remain unread",
        follow_visible=False,
        follow_recommendations=["use_item: unread revision"],
    )
    provider = RotatingPromptConfigProvider([pinned_snapshot, unread_snapshot])
    manager = ContextManager(prompt_config_provider=provider)  # type: ignore[arg-type]
    build_kwargs = {
        "observation": {"health": 20, "inventory": []},
        "task_memory": [],
        "task_spec": {"goal": "Follow a target."},
        "allowed_actions": ["follow", "query_inventory"],
    }

    first = await manager.build(**build_kwargs)
    second = await manager.build(**build_kwargs)

    assert provider.calls == 1
    assert first.messages[0]["content"].startswith("configured pinned system prompt")
    assert second.messages[0]["content"].startswith("configured pinned system prompt")
    assert first.prompt_visible_actions == ["follow"]
    assert second.prompt_visible_actions == ["follow"]
    assert first.action_recommendations["follow"] == ["use_item: pinned revision"]
    assert second.action_recommendations["follow"] == ["use_item: pinned revision"]
    assert (
        first.prompt_sections["prompt_configuration"]["revision"]
        == second.prompt_sections["prompt_configuration"]["revision"]
    )
    assert first.prompt_sections["prompt_configuration"]["source"] == (
        "database_prompt_config_provider"
    )
    assert second.prompt_sections["prompt_configuration"]["source"] == (
        "database_prompt_config_provider_pinned"
    )
    assert first.prompt_sections["prompt_configuration"]["hot_reload_enabled"] is False
    assert second.prompt_sections["prompt_configuration"]["hot_reload_enabled"] is False
    assert first.prompt_sections["prompt_configuration"]["snapshot_mode"] == "run_pinned"
    assert second.prompt_sections["prompt_configuration"]["snapshot_mode"] == "run_pinned"


@pytest.mark.anyio
async def test_context_manager_pins_when_live_configuration_turns_hot_reload_off() -> None:
    live_snapshot = _prompt_snapshot(
        version=1,
        system_prompt="configured live system prompt",
        follow_visible=True,
        follow_recommendations=["use_item: live revision"],
    )
    pinning_snapshot = _prompt_snapshot(
        version=2,
        system_prompt="configured newly pinned system prompt",
        follow_visible=False,
        follow_recommendations=["use_item: pinned revision"],
        hot_reload_enabled=False,
    )
    unread_snapshot = _prompt_snapshot(
        version=3,
        system_prompt="configured later system prompt",
        follow_visible=True,
        follow_recommendations=["use_item: later revision"],
    )
    provider = RotatingPromptConfigProvider(
        [live_snapshot, pinning_snapshot, unread_snapshot]
    )
    manager = ContextManager(prompt_config_provider=provider)  # type: ignore[arg-type]
    build_kwargs = {
        "observation": {"health": 20, "inventory": []},
        "task_memory": [],
        "task_spec": {"goal": "Follow a target."},
        "allowed_actions": ["follow", "query_inventory"],
    }

    first = await manager.build(**build_kwargs)
    second = await manager.build(**build_kwargs)
    third = await manager.build(**build_kwargs)

    assert provider.calls == 2
    assert first.messages[0]["content"].startswith("configured live system prompt")
    assert second.messages[0]["content"].startswith("configured newly pinned system prompt")
    assert third.messages[0]["content"].startswith("configured newly pinned system prompt")
    assert first.prompt_sections["prompt_configuration"]["snapshot_mode"] == "live"
    assert second.prompt_sections["prompt_configuration"]["snapshot_mode"] == "run_pinned"
    assert third.prompt_sections["prompt_configuration"]["snapshot_mode"] == "run_pinned"
    assert (
        first.prompt_sections["prompt_configuration"]["revision"]
        != second.prompt_sections["prompt_configuration"]["revision"]
    )
    assert (
        second.prompt_sections["prompt_configuration"]["revision"]
        == third.prompt_sections["prompt_configuration"]["revision"]
    )
    assert third.prompt_sections["prompt_configuration"]["source"] == (
        "database_prompt_config_provider_pinned"
    )


@pytest.mark.anyio
async def test_context_manager_does_not_pin_code_fallback_after_provider_failure() -> None:
    configured_snapshot = _prompt_snapshot(
        version=4,
        system_prompt="configured recovered system prompt",
        follow_visible=True,
        follow_recommendations=["use_item: recovered revision"],
        hot_reload_enabled=False,
    )
    provider = RecoveringPromptConfigProvider(configured_snapshot)
    manager = ContextManager(prompt_config_provider=provider)  # type: ignore[arg-type]
    build_kwargs = {
        "observation": {"health": 20, "inventory": []},
        "task_memory": [],
        "task_spec": {"goal": "Follow a target."},
        "allowed_actions": ["follow"],
    }

    fallback = await manager.build(**build_kwargs)
    recovered = await manager.build(**build_kwargs)
    pinned = await manager.build(**build_kwargs)

    assert provider.calls == 2
    assert fallback.prompt_sections["prompt_configuration"]["source"] == "code_fallback"
    assert fallback.prompt_sections["prompt_configuration"]["snapshot_mode"] == "fallback"
    assert fallback.prompt_sections["prompt_configuration"]["hot_reload_enabled"] is None
    assert recovered.messages[0]["content"].startswith("configured recovered system prompt")
    assert pinned.messages[0]["content"].startswith("configured recovered system prompt")
    assert recovered.prompt_sections["prompt_configuration"]["snapshot_mode"] == "run_pinned"
    assert pinned.prompt_sections["prompt_configuration"]["source"] == (
        "database_prompt_config_provider_pinned"
    )


@pytest.mark.anyio
async def test_context_manager_exposes_knowledge_tools_without_auto_injection() -> None:
    manager = ContextManager()

    result = await manager.build(
        observation={
            "health": 20,
            "food": 20,
            "inventory": [],
            "nearby_blocks": [{"name": "oak_log"}],
        },
        task_memory=["Failed once because no crafting table was available."],
        task_spec={
            "task_id": "craft_wooden_pickaxe",
            "goal": "Craft a wooden pickaxe from log, plank, and crafting table.",
            "allowed_actions": ["query_inventory", "scan_blocks", "dig_block_at"],
            "manifest_allowed_actions": ["fight_entity"],
            "benchmark": {
                "seed": 1,
                "scripted_actions": [
                    {"type": "scan_blocks", "args": {"block": "oak_log"}},
                    {
                        "type": "dig_block_at",
                        "args": {"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}},
                    },
                ],
                "initial_state": {"nearby_blocks": [{"name": "oak_log"}]},
            },
        },
        allowed_actions=[
            "resolve_terms",
            "get_recipe",
            "retrieve_docs",
            "query_inventory",
            "scan_blocks",
            "dig_block_at",
        ],
    )

    payload = json.loads(result.messages[1]["content"])

    assert isinstance(result, ContextBuildResult)
    assert payload["resolved_terms"] == []
    assert payload["retrieved_docs"] == []
    stable = result.prompt_sections["stable_system_payload"]
    assert stable["knowledge_tool_contract"]["available_tools"] == [
        "resolve_terms",
        "get_recipe",
        "retrieve_docs",
    ]
    assert stable["action_contract"]["allowed_actions"] == [
        "resolve_terms",
        "get_recipe",
        "retrieve_docs",
        "query_inventory",
        "scan_blocks",
        "dig_block_at",
    ]
    stable_task = result.prompt_sections["stable_task_payload"]
    assert "allowed_actions" not in stable_task["task"]
    assert "manifest_allowed_actions" not in stable_task["task"]
    assert "task" not in payload
    assert "task_objective" not in payload
    assert "action_contract" not in payload
    assert "knowledge_tool_contract" not in payload
    assert "reasoning_summary" in stable["action_contract"]["output_format"]
    assert "knowledge_need" in stable["action_contract"]["output_format"]
    assert "memory_update" in stable["action_contract"]["output_format"]
    assert any(
        "durable entity-specific fact" in rule
        for rule in stable["action_contract"]["rules"]
    )
    assert any(
        "scan_entities is a read-only snapshot" in rule
        and "move a meaningful distance" in rule
        for rule in stable["action_contract"]["rules"]
    )
    assert stable["action_contract"]["output_format"]["action"]["type"] == "action_name"
    assert "reasoning_summary" in result.messages[0]["content"]
    assert "scripted_actions" not in stable_task["task"]["benchmark"]
    assert "initial_state" not in stable_task["task"]["benchmark"]
    assert "raw Mineflayer" in result.messages[0]["content"]


@pytest.mark.anyio
async def test_context_manager_exposes_selected_agent_memory_and_source_handles() -> None:
    run_context = RunContextMemory()
    scan_action = HarnessAction(type="scan_entities", args={"entity": "sheep"})
    scan_result = {
        "ok": True,
        "entities": [
            {
                "entity_id": 68,
                "name": "sheep",
                "details": {
                    "metadata": {"wool": 28},
                    "metadata_decoded": {"wool": {"color": "brown", "is_sheared": True}},
                    "metadata_decoder": {
                        "available": True,
                        "minecraft_version": "1.20.1",
                        "recognized_fields": ["wool"],
                    },
                },
            }
        ],
    }
    run_context.memory.record_source(
        step_index=2,
        action=scan_action,
        result=scan_result,
    )
    run_context.memory.apply_updates(
        [
            MemoryUpdate(
                memory_key="entity:68/wool_state",
                source_ref="step:2/scan_entities/entity:68",
                paths=["/details/metadata_decoded/wool/color"],
                note="This is not a white sheep.",
            )
        ],
        decision_step_index=3,
    )

    result = await ContextManager().build(
        observation={"inventory": [], "nearby_entities": []},
        task_memory=[],
        task_spec={"goal": "Obtain white wool."},
        allowed_actions=["scan_entities", "query_inventory"],
        previous_step={
            "step_index": 2,
            "action": scan_action.model_dump(mode="json"),
            "action_result": scan_result,
        },
        run_context=run_context,
        step_index=3,
    )

    payload = json.loads(result.messages[1]["content"])
    previous = payload["compact_evidence"]["previous_step"]
    assert previous["nearest_entities"][0]["details"]["metadata_decoded"]["wool"] == {
        "color": "brown",
        "is_sheared": True,
    }
    assert previous["memory_sources"][1]["source_ref"] == ("step:2/scan_entities/entity:68")
    assert payload["run_context"]["memory"]["entries"][0]["memory_key"] == ("entity:68/wool_state")


@pytest.mark.anyio
async def test_context_manager_can_auto_retrieve_knowledge_for_ablation() -> None:
    manager = ContextManager(policy=ContextPolicy(auto_retrieve_knowledge=True))

    result = await manager.build(
        observation={
            "health": 20,
            "food": 20,
            "inventory": [],
            "nearby_blocks": [{"name": "oak_log"}],
        },
        task_memory=[],
        task_spec={
            "task_id": "craft_wooden_pickaxe",
            "goal": "Craft a wooden pickaxe from log, plank, and crafting table.",
        },
        allowed_actions=["query_inventory"],
    )

    payload = json.loads(result.messages[1]["content"])
    resolved_ids = {term["canonical_id"] for term in payload["resolved_terms"]}

    assert "wooden_pickaxe" in resolved_ids
    assert "oak_log" in resolved_ids
    assert "oak_planks" in resolved_ids
    assert "crafting_table" in resolved_ids
    assert payload["retrieved_docs"]


@pytest.mark.anyio
async def test_context_manager_exposes_finish_as_evaluation_request() -> None:
    """Prompt semantics should separate agent finish intent from evaluator success."""

    result = await ContextManager().build(
        observation={"health": 20, "food": 20, "inventory": [], "nearby_blocks": []},
        task_memory=[],
        task_spec={"task_id": "creative_scene", "goal": "Build a visible scene."},
        allowed_actions=["request_visual_snapshot", "submit_for_evaluation"],
    )

    stable = result.prompt_sections["stable_system_payload"]
    termination = stable["termination_contract"]
    finish_guide = next(
        guide
        for guide in stable["available_action_primitives"]
        if guide["type"] == "submit_for_evaluation"
    )
    assert termination == {
        "agent_finish_action": "submit_for_evaluation",
        "finish_enabled": True,
        "success_authority": "task_evaluator",
        "agent_semantics": "request_evaluation_not_declare_success",
        "without_evaluator": "terminate_as_unverified",
        "harness_safeguards": ["max_steps", "max_runtime", "runtime_termination"],
    }
    assert "concrete evidence" in str(finish_guide["when_to_use"])
    assert "evaluator, not you" in result.messages[0]["content"]


@pytest.mark.anyio
async def test_context_manager_hides_internal_initial_inventory_field() -> None:
    manager = ContextManager()

    result = await manager.build(
        observation={
            "health": 20,
            "food": 20,
            "inventory": [{"name": "oak_log", "count": 1}],
            "nearby_blocks": [],
            "nearby_entities": [],
        },
        task_memory=[],
        task_spec={
            "task_id": "minedojo_harvest_oak_log",
            "goal": "Harvest one oak log.",
            "require_inventory_delta": True,
            "_initial_inventory": [],
            "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        },
        allowed_actions=["query_inventory"],
    )

    payload = json.loads(result.messages[1]["content"])
    stable_task = result.prompt_sections["stable_task_payload"]

    assert "_initial_inventory" not in stable_task["task"]
    assert stable_task["task_objective"] == {
        "task_id": "minedojo_harvest_oak_log",
        "goal": "Harvest one oak log.",
        "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        "success_criteria": None,
        "completion_authority": "task_evaluator",
    }
    assert payload["task_progress"]["goal"] == "Harvest one oak log."
    assert payload["task_progress"]["completion_status"] == (
        "goal_satisfied_by_current_observation"
    )
    assert payload["task_progress"]["checks"] == payload["compact_evidence"]["goal_progress"]
    assert payload["compact_evidence"]["goal_progress"][0]["current_delta"] == 1
    assert "new oak_log +1/+1" in payload["state_summary"]


@pytest.mark.anyio
async def test_context_manager_injects_previous_step_as_react_observation() -> None:
    """The next prompt carries compressed previous action evidence for ReAct loops."""

    manager = ContextManager()

    result = await manager.build(
        observation={
            "health": 20,
            "food": 20,
            "inventory": [],
            "nearby_blocks": [],
        },
        task_memory=[],
        task_spec={
            "task_id": "minedojo_harvest_oak_log",
            "goal": "Harvest one oak log.",
        },
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
        previous_step={
            "step_index": 0,
            "action": {"type": "scan_blocks", "args": {"block": "oak_log", "max_distance": 16}},
            "action_result": {
                "ok": True,
                "action_type": "scan_blocks",
                "blocks": [
                    {
                        "name": "oak_log",
                        "position": {"x": 7, "y": 65, "z": 1},
                        "distance": 7.7,
                        "can_dig": False,
                    }
                ],
            },
        },
    )

    payload = json.loads(result.messages[1]["content"])
    compact_evidence = payload["compact_evidence"]
    previous_step = compact_evidence["previous_step"]

    assert "observation" not in payload
    assert previous_step["step_index"] == 0
    assert previous_step["action_type"] == "scan_blocks"
    assert previous_step["nearest_targets"][0]["position"] == {"x": 7.0, "y": 65.0, "z": 1.0}
    assert previous_step["nearest_targets"][0]["can_dig"] is False
    assert "state_summary" not in result.prompt_sections["stable_system_payload"]
    assert "compact_evidence" not in result.prompt_sections["stable_system_payload"]


@pytest.mark.anyio
async def test_context_manager_keeps_system_prefix_stable_across_dynamic_turns() -> None:
    """Task state and history must not invalidate the cacheable system-message prefix."""

    manager = ContextManager()
    first = await manager.build(
        observation={"health": 20, "inventory": []},
        task_memory=[],
        task_spec={"goal": "Collect one oak log."},
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
    )
    second = await manager.build(
        observation={"health": 11, "inventory": [{"name": "dirt", "count": 4}]},
        task_memory=["The first route was blocked."],
        task_spec={"goal": "Collect one oak log."},
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
        previous_step={
            "step_index": 2,
            "action": {"type": "move_to", "args": {"position": {"x": 8, "y": 65, "z": 4}}},
            "action_result": {"ok": False, "progress_status": "no_path"},
        },
    )

    assert first.messages[0] == second.messages[0]
    assert first.messages[1] != second.messages[1]


@pytest.mark.anyio
async def test_context_manager_places_task_at_end_of_cacheable_system_prefix() -> None:
    """A task change invalidates the prefix, while progress changes stay in the user turn."""

    manager = ContextManager()
    first = await manager.build(
        observation={"health": 20, "inventory": []},
        task_memory=[],
        task_spec={
            "task_id": "collect_oak",
            "goal": "Collect one oak log.",
            "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        },
        allowed_actions=["query_inventory"],
    )
    second = await manager.build(
        observation={"health": 20, "inventory": [{"name": "oak_log", "count": 1}]},
        task_memory=[],
        task_spec={
            "task_id": "collect_oak",
            "goal": "Collect one oak log.",
            "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        },
        allowed_actions=["query_inventory"],
    )
    different_task = await manager.build(
        observation={"health": 20, "inventory": []},
        task_memory=[],
        task_spec={
            "task_id": "collect_stone",
            "goal": "Collect one stone.",
            "verifier": {"type": "inventory_contains", "item": "stone", "count": 1},
        },
        allowed_actions=["query_inventory"],
    )

    assert first.messages[0] == second.messages[0]
    assert first.messages[1] != second.messages[1]
    assert first.messages[0] != different_task.messages[0]
    assert first.messages[0]["content"].endswith(first.prompt_sections["stable_task_prompt"])
    assert "task" not in first.prompt_sections["user_payload"]
    assert "task_objective" not in first.prompt_sections["user_payload"]
    assert (
        first.prompt_sections["user_payload"]["task_progress"]
        != (second.prompt_sections["user_payload"]["task_progress"])
    )


@pytest.mark.anyio
async def test_context_manager_injects_visual_frame_without_persisting_base64(
    tmp_path: Path,
) -> None:
    """A requested frame reaches Qwen while the audit message retains metadata only."""

    frame = tmp_path / "snapshot.jpg"
    frame.write_bytes(b"bounded-test-frame")
    manager = ContextManager()

    result = await manager.build(
        observation={"health": 20, "inventory": []},
        task_memory=[],
        task_spec={"goal": "Inspect the structure visually."},
        allowed_actions=["request_visual_snapshot", "place_block"],
        previous_step={
            "step_index": 4,
            "action": {"type": "request_visual_snapshot", "args": {}},
            "action_result": {
                "ok": True,
                "snapshot": {
                    "image": str(frame),
                    "artifact_path": str(frame),
                    "format": "jpeg",
                    "mime_type": "image/jpeg",
                    "width": 1280,
                    "height": 720,
                    "sha256": "abc123",
                },
            },
        },
    )

    model_content = result.messages[1]["content"]
    audit_content = result.audit_messages[1]["content"]
    assert isinstance(model_content, list)
    assert model_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert isinstance(audit_content, list)
    assert "base64" not in json.dumps(audit_content)
    assert audit_content[1]["artifact"]["artifact_path"] == str(frame.resolve())
    assert result.prompt_sections["visual_input"]["source_step_index"] == 4


@pytest.mark.anyio
async def test_context_manager_does_not_duplicate_latest_knowledge_result() -> None:
    """The latest result stays in ReAct evidence and returns to the ledger on later turns."""

    manager = ContextManager()
    run_context = RunContextMemory()
    action = HarnessAction(type="get_recipe", args={"item": "glass"})
    result = {
        "ok": True,
        "tool": "get_recipe",
        "item": "glass",
        "recipe": {
            "output": "glass",
            "output_count": 1,
            "station": "furnace",
            "ingredients": [{"item_id": "sand", "count": 1}],
        },
    }
    run_context.knowledge.record(
        step_index=0,
        action=action,
        result=result,
        observation={},
        task_spec={"goal": "Obtain glass."},
    )
    latest = await manager.build(
        observation={"inventory": []},
        task_memory=[],
        task_spec={"goal": "Obtain glass."},
        allowed_actions=["get_recipe", "query_inventory"],
        previous_step={
            "step_index": 0,
            "action": action.model_dump(mode="json"),
            "action_result": result,
        },
        run_context=run_context,
    )
    later = await manager.build(
        observation={"inventory": []},
        task_memory=[],
        task_spec={"goal": "Obtain glass."},
        allowed_actions=["get_recipe", "query_inventory"],
        previous_step={
            "step_index": 1,
            "action": {"type": "query_inventory", "args": {}},
            "action_result": {"ok": True, "inventory": []},
        },
        run_context=run_context,
    )

    latest_payload = json.loads(latest.messages[1]["content"])
    later_payload = json.loads(later.messages[1]["content"])
    assert latest_payload["compact_evidence"]["previous_step"]["recipe"]["output"] == "glass"
    assert latest_payload["run_context"]["knowledge"]["entries"] == []
    assert later_payload["run_context"]["knowledge"]["entries"][0]["result"]["item"] == "glass"


def _context_skill(name: str, version: str = "0.1.0") -> SkillSpec:
    """Build one promoted skill used to exercise context-level deduplication."""

    return SkillSpec(
        name=name,
        version=version,
        description="Collect an oak log using evidence-backed navigation.",
        triggers=["oak_log", "harvest", "collect"],
        strategy_summary="Find a reachable oak log, approach it, dig it, and verify pickup.",
        status=SkillStatus.promoted,
    )


@pytest.mark.anyio
async def test_context_manager_injects_the_same_skill_only_once_per_visible_context() -> None:
    """A retrieved skill moves into run_context and is not duplicated on later turns."""

    skill = _context_skill("collect_wood")
    snapshot = SkillLibrarySnapshot(
        revision="test-revision",
        captured_at="2026-07-25T00:00:00Z",
        skills=(skill,),
    )
    manager = ContextManager(skill_library=snapshot)
    run_context = RunContextMemory()

    first = await manager.build(
        observation={"inventory": [], "nearby_blocks": [{"name": "oak_log"}]},
        task_memory=[],
        task_spec={"task_id": "harvest_oak_log", "goal": "Harvest one oak log."},
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
        run_context=run_context,
        step_index=0,
    )
    second = await manager.build(
        observation={"inventory": [], "nearby_blocks": [{"name": "oak_log"}]},
        task_memory=[],
        task_spec={"task_id": "harvest_oak_log", "goal": "Harvest one oak log."},
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
        run_context=run_context,
        step_index=1,
    )

    first_payload = json.loads(first.messages[1]["content"])
    second_payload = json.loads(second.messages[1]["content"])
    assert [item["name"] for item in first_payload["retrieved_skills"]] == ["collect_wood"]
    assert first_payload["run_context"]["skills"]["entries"] == []
    assert second_payload["retrieved_skills"] == []
    assert [item["name"] for item in second_payload["run_context"]["skills"]["entries"]] == [
        "collect_wood"
    ]
    assert second.prompt_sections["skill_injection"]["skipped"] == [
        {
            "identity": "collect_wood@0.1.0",
            "name": "collect_wood",
            "version": "0.1.0",
            "reason": "already_present_in_context",
        }
    ]
    assert run_context.skills.entries["collect_wood@0.1.0"].injection_count == 1


@pytest.mark.anyio
async def test_context_manager_filters_skills_below_task_relevance_threshold() -> None:
    """Shared biome/actions must not make an unrelated Skill eligible for injection."""

    wool_skill = SkillSpec(
        name="shear_white_wool",
        version="0.1.0",
        description="Find a sheep and shear it to obtain white wool.",
        triggers=[
            "harvest_wool_extreme_hills_with_shears",
            "sheep",
            "shears",
            "white_wool",
        ],
        strategy_summary="Scan for a sheep, approach it, use shears, and collect white_wool.",
        task_scope=["harvest", "extreme_hills", "white_wool"],
        dependencies=["sheep", "shears", "white_wool"],
        status=SkillStatus.promoted,
    )
    milk_skill = SkillSpec(
        name="collect_milk",
        version="0.1.0",
        description="Find a cow in extreme hills and fill an empty bucket.",
        triggers=["harvest", "extreme_hills", "cow", "milk_bucket"],
        strategy_summary="Scan for a cow, approach it, and use an empty bucket.",
        task_scope=["harvest", "extreme_hills", "milk_bucket"],
        dependencies=["cow", "bucket", "milk_bucket"],
        status=SkillStatus.promoted,
    )
    manager = ContextManager(
        policy=ContextPolicy(min_skill_relevance=0.5),
        skill_library=SkillLibrarySnapshot(
            revision="threshold",
            captured_at="2026-07-25T00:00:00Z",
            skills=(wool_skill, milk_skill),
        ),
    )

    result = await manager.build(
        observation={"inventory": [{"name": "shears", "count": 1}]},
        task_memory=[],
        task_spec={
            "task_id": "harvest_wool_extreme_hills_with_shears",
            "goal": "shear a sheep in extreme hills with shears",
            "description": "Obtain white wool by shearing a sheep.",
            "category": "harvest",
            "family": "Harvest",
            "knowledge_tags": [
                "minecraft:term/extreme",
                "minecraft:term/hills",
                "minecraft:term/sheep",
                "minecraft:term/shears",
                "minecraft:term/wool",
            ],
            "verifier": {"type": "inventory_contains", "item": "white_wool"},
        },
        allowed_actions=["scan_entities", "move_to", "use_item"],
    )

    assert [skill.name for skill in result.retrieved_skills] == ["shear_white_wool"]
    audit = result.prompt_sections["skill_injection"]
    assert audit["relevance_threshold"] == 0.5
    assert [item["name"] for item in audit["filtered_by_relevance"]] == ["collect_milk"]
    assert audit["filtered_by_relevance"][0]["reason"] == ("below_relevance_threshold")


@pytest.mark.anyio
async def test_context_manager_can_reinject_relevant_skill_after_context_eviction() -> None:
    """Thresholding must not become a run-global once-only Skill policy."""

    skill = _context_skill("collect_wood")
    manager = ContextManager(
        policy=ContextPolicy(max_skill_ledger_chars=1),
        skill_library=SkillLibrarySnapshot(
            revision="reinject",
            captured_at="2026-07-25T00:00:00Z",
            skills=(skill,),
        ),
    )
    run_context = RunContextMemory()
    common = {
        "observation": {"nearby_blocks": [{"name": "oak_log"}]},
        "task_memory": [],
        "task_spec": {"task_id": "harvest_oak_log", "goal": "Harvest one oak log."},
        "allowed_actions": ["scan_blocks", "move_to", "dig_block_at"],
        "run_context": run_context,
    }

    first = await manager.build(**common, step_index=0)
    second = await manager.build(**common, step_index=1)

    assert [skill.name for skill in first.retrieved_skills] == ["collect_wood"]
    assert [skill.name for skill in second.retrieved_skills] == ["collect_wood"]
    assert run_context.skills.entries["collect_wood@0.1.0"].injection_count == 2


@pytest.mark.anyio
async def test_context_manager_deduplicates_search_results_but_allows_a_new_version() -> None:
    """Duplicate identities are skipped while a distinct skill version can be injected."""

    old_skill = _context_skill("collect_wood", "0.1.0")
    run_context = RunContextMemory()
    duplicate_manager = ContextManager(
        skill_library=SkillLibrarySnapshot(
            revision="duplicate",
            captured_at="2026-07-25T00:00:00Z",
            skills=(old_skill, old_skill),
        )
    )
    first = await duplicate_manager.build(
        observation={"nearby_blocks": [{"name": "oak_log"}]},
        task_memory=[],
        task_spec={"goal": "Harvest an oak log."},
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
        run_context=run_context,
        step_index=0,
    )

    new_skill = _context_skill("collect_wood", "0.2.0")
    upgraded = await ContextManager(
        skill_library=SkillLibrarySnapshot(
            revision="upgraded",
            captured_at="2026-07-25T00:01:00Z",
            skills=(new_skill,),
        )
    ).build(
        observation={"nearby_blocks": [{"name": "oak_log"}]},
        task_memory=[],
        task_spec={"goal": "Harvest an oak log."},
        allowed_actions=["scan_blocks", "move_to", "dig_block_at"],
        run_context=run_context,
        step_index=1,
    )

    assert [skill.version for skill in first.retrieved_skills] == ["0.1.0"]
    assert first.prompt_sections["skill_injection"]["skipped"][0]["reason"] == (
        "duplicate_search_result"
    )
    assert [skill.version for skill in upgraded.retrieved_skills] == ["0.2.0"]
