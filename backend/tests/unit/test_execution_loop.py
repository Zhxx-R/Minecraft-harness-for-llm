from typing import Any
from pathlib import Path

import pytest

from mc_agent_harness.configuration.defaults import (
    ACTION_PROMPT_KIND,
    SYSTEM_PROMPT_KEY,
    SYSTEM_PROMPT_KIND,
    default_action_payload,
)
from mc_agent_harness.configuration.service import PromptConfigEntry, PromptConfigSnapshot
from mc_agent_harness.harness.context_manager import ContextManager
from mc_agent_harness.harness.evaluation import EvaluationRecorder
from mc_agent_harness.harness.execution_loop import ExecutionBudget, ExecutionLoop
from mc_agent_harness.harness.planner import AgentPlanner
from mc_agent_harness.harness.action_repair import (
    ActionGenerationTimeout,
    ActionRepairConfig,
    ActionRepairFailed,
    ActionRepairPolicy,
)
from mc_agent_harness.harness.tool_registry import ToolRegistry
from mc_agent_harness.models.router import ModelCompletion, ModelProfile, ModelRouter
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import HarnessAction


class FakeRuntime(GameRuntime):
    """In-memory runtime that records reset specs and actions for loop tests."""

    def __init__(self) -> None:
        self.reset_spec: dict[str, Any] | None = None
        self.actions: list[HarnessAction] = []

    async def reset(self, task_spec: dict[str, Any]) -> None:
        """Store the reset task spec."""

        self.reset_spec = task_spec

    async def observe(self) -> dict[str, Any]:
        """Return one stable observation suitable for a controlled action."""

        return {
            "health": 20,
            "food": 20,
            "inventory": [],
            "nearby_blocks": [{"name": "oak_log"}],
            "nearby_entities": [],
        }

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Record one dispatched action and return a fake runtime result."""

        self.actions.append(action)
        return {"ok": True, "inventory": []}

    async def snapshot(self) -> dict[str, Any]:
        """Return an empty visual snapshot."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Close the fake runtime."""


class FakeReachabilityRuntime(FakeRuntime):
    """Runtime that returns pathfinding diagnostics for a move_to action."""

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Return a failed move_to result with compact pathfinder evidence."""

        self.actions.append(action)
        return {
            "ok": False,
            "action_type": action.type,
            "error_code": "no_path",
            "target": {"x": 3, "y": 68, "z": 4},
            "tolerance": 1.5,
            "timeout_ms": 8000,
            "diagnosis": "The target is not reachable under the current movement policy.",
            "state_summary": "The target is not reachable. Try nearest reachable ground.",
            "movement_policy": {"can_dig": False, "can_place": False, "max_drop_down": 3},
            "suggested_affordances": [{"action": "scan_blocks", "when": "inspect terrain"}],
            "nearest_reachable_position": {"x": 3, "y": 65, "z": 4},
            "target_height_delta": 3,
            "path_summary": {
                "status": "partial",
                "path_length": 1,
                "last_node": {"x": 3, "y": 65, "z": 4},
                "visited_nodes": 42,
            },
            "path_resets": [],
            "observation": {
                "health": 20,
                "food": 20,
                "inventory": [],
                "position": {"x": 2, "y": 65, "z": 4},
                "nearby_blocks": [],
                "nearby_entities": [],
            },
        }


class FakeEntityMetadataRuntime(FakeRuntime):
    """Runtime double exposing one semantically decoded sheep scan."""

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        self.actions.append(action)
        if action.type == "scan_entities":
            return {
                "ok": True,
                "action_type": "scan_entities",
                "entities": [
                    {
                        "entity_id": 68,
                        "name": "sheep",
                        "details": {
                            "metadata_decoded": {
                                "wool": {
                                    "color": "brown",
                                    "is_sheared": True,
                                }
                            }
                        },
                    }
                ],
            }
        return {"ok": True, "inventory": []}


class FakeFollowHandoffRuntime(FakeRuntime):
    """Runtime double whose active follow closes distance between observations."""

    def __init__(self) -> None:
        super().__init__()
        self.active_follow = False
        self.follow_observation_count = 0
        self.last_distance = 8.0

    async def observe(self) -> dict[str, Any]:
        if self.active_follow:
            self.follow_observation_count += 1
            distances = [8.0, 4.0, 1.5]
            self.last_distance = distances[
                min(self.follow_observation_count - 1, len(distances) - 1)
            ]
        return {
            "health": 20,
            "food": 20,
            "inventory": [{"name": "shears", "count": 1}],
            "active_follow": (
                {
                    "active": True,
                    "target": {
                        "id": 72,
                        "name": "sheep",
                        "type": "animal",
                    },
                    "follow_distance": 1.25,
                    "until": "next_action_received",
                }
                if self.active_follow
                else None
            ),
            "nearby_entities": [
                {
                    "entity_id": 72,
                    "id": 72,
                    "name": "sheep",
                    "type": "animal",
                    "distance": self.last_distance,
                }
            ],
        }

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        self.actions.append(action)
        if action.type == "follow":
            self.active_follow = True
            self.follow_observation_count = 0
            self.last_distance = 8.0
            return {
                "ok": True,
                "action_type": "follow",
                "status": "following",
                "persistent": True,
                "target": {
                    "entity_id": 72,
                    "id": 72,
                    "name": "sheep",
                    "type": "animal",
                    "distance": 8.0,
                },
                "follow_distance": 1.25,
                "active_follow": {
                    "active": True,
                    "target": {
                        "id": 72,
                        "name": "sheep",
                        "type": "animal",
                    },
                    "follow_distance": 1.25,
                    "until": "next_action_received",
                },
                "recommended_next_actions": [
                    (
                        "use_item: Use when the task requires using the held item "
                        "on the followed entity."
                    ),
                ],
            }
        self.active_follow = False
        return {
            "ok": True,
            "action_type": action.type,
            "entity_id": action.args.get("entity_id"),
            "distance_at_dispatch": self.last_distance,
        }


class FakeInitialVisualRuntime(FakeRuntime):
    """Runtime double that returns one local image for the harness-owned initial visual step."""

    def __init__(self, image_path: str) -> None:
        super().__init__()
        self.image_path = image_path

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Return a visual artifact for snapshot actions and delegate ordinary actions."""

        self.actions.append(action)
        if action.type == "request_visual_snapshot":
            return {
                "ok": True,
                "action_type": action.type,
                "snapshot": {
                    "artifact_path": self.image_path,
                    "format": "jpeg",
                    "mime_type": "image/jpeg",
                },
                "observation": await self.observe(),
            }
        return {"ok": True, "inventory": []}


class FakeRpcTimeoutRuntime(FakeRuntime):
    """Runtime double that exposes a responsive worker with an unknown action result."""

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Return the structured timeout contract emitted by MineflayerClient."""

        self.actions.append(action)
        return {
            "ok": False,
            "action_type": action.type,
            "error_code": "rpc_timeout",
            "message": "worker action result was not returned",
            "recoverable": True,
            "terminated": False,
            "requires_worker_restart": True,
            "worker_health": {"responsive": True},
            "observation": await self.observe(),
        }


class FakeProvider:
    """Deterministic provider that returns one configured action JSON string."""

    def __init__(self, action_json: str | list[str]) -> None:
        self.action_json = [action_json] if isinstance(action_json, str) else list(action_json)
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Return the configured action and ignore provider-only metadata."""

        _ = (messages, profile, response_schema)
        index = min(self.calls, len(self.action_json) - 1)
        self.calls += 1
        return ModelCompletion(
            content=self.action_json[index],
            raw_response={"id": f"fake_call_{self.calls}", "model": profile.id},
        )


class FixedPromptConfigProvider:
    """Expose one immutable prompt snapshot to every decision turn."""

    def __init__(self, snapshot: PromptConfigSnapshot) -> None:
        self.value = snapshot
        self.calls = 0

    def snapshot(self) -> PromptConfigSnapshot:
        self.calls += 1
        return self.value


def _follow_prompt_snapshot() -> PromptConfigSnapshot:
    follow_payload = default_action_payload("follow")
    follow_payload["recommended_next_actions"] = [
        "use_item: configured handoff guidance"
    ]
    return PromptConfigSnapshot(
        system=PromptConfigEntry(
            kind=SYSTEM_PROMPT_KIND,
            config_key=SYSTEM_PROMPT_KEY,
            display_name="Agent system prompt",
            enabled=True,
            payload={"content": "configured runtime system prompt"},
            version=4,
            persisted=True,
        ),
        actions={
            "follow": PromptConfigEntry(
                kind=ACTION_PROMPT_KIND,
                config_key="follow",
                display_name="follow",
                enabled=True,
                payload=follow_payload,
                version=7,
                persisted=True,
            )
        },
    )


class TimeoutThenSuccessProvider:
    """Fake provider that times out a configured number of times before succeeding."""

    def __init__(self, timeout_count: int, action_json: str) -> None:
        self.timeout_count = timeout_count
        self.action_json = action_json
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Raise TimeoutError until the configured retry threshold is reached."""

        _ = (messages, profile, response_schema)
        self.calls += 1
        if self.calls <= self.timeout_count:
            raise TimeoutError("model read timed out")
        return ModelCompletion(
            content=self.action_json,
            raw_response={"id": f"fake_call_{self.calls}", "model": profile.id},
        )


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_execution_loop_applies_same_turn_configured_action_recommendations() -> None:
    runtime = FakeFollowHandoffRuntime()
    recorder = EvaluationRecorder()
    prompt_provider = FixedPromptConfigProvider(_follow_prompt_snapshot())
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(
            provider=FakeProvider(
                '{"type":"follow","args":{"entity_id":72,"follow_distance":1.25}}'
            )
        ),
        context_manager=ContextManager(  # type: ignore[arg-type]
            prompt_config_provider=prompt_provider
        ),
        tool_registry=ToolRegistry(["follow"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "configured_follow",
        task_spec={"goal": "Follow the selected sheep."},
    )

    action_result = result.steps[0].action_result
    context_event = next(
        event for event in recorder.events if event.event_type == "context_built"
    )
    config_revision = context_event.payload["prompt_sections"][
        "prompt_configuration"
    ]["revision"]
    assert prompt_provider.calls == 1
    assert action_result["runtime_recommended_next_actions"][0].startswith("use_item:")
    assert action_result["recommended_next_actions"] == [
        "use_item: configured handoff guidance"
    ]
    assert action_result["recommended_next_actions_source"] == "prompt_configuration"
    assert action_result["prompt_config_revision"] == config_revision
    assert action_result["recommendation_config_version"]["version"] == 7


@pytest.mark.anyio
async def test_action_repair_keeps_the_turns_configured_prompt_visibility() -> None:
    runtime = FakeFollowHandoffRuntime()
    recorder = EvaluationRecorder()
    prompt_provider = FixedPromptConfigProvider(_follow_prompt_snapshot())
    model_provider = FakeProvider(
        [
            "not-json",
            '{"type":"follow","args":{"entity_id":72,"follow_distance":1.25}}',
        ]
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=model_provider),
        context_manager=ContextManager(  # type: ignore[arg-type]
            prompt_config_provider=prompt_provider
        ),
        tool_registry=ToolRegistry(["follow", "query_inventory"]),
        recorder=recorder,
        action_repair_policy=ActionRepairPolicy(
            ActionRepairConfig(max_attempts=1, model_timeout_retries=0)
        ),
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "repair_with_configured_visibility",
        task_spec={"goal": "Follow the selected sheep."},
    )

    repair_event = next(
        event for event in recorder.events if event.event_type == "model_repair_attempt"
    )
    assert result.steps[0].action.type == "follow"
    assert model_provider.calls == 2
    assert prompt_provider.calls == 1
    assert repair_event.payload["allowed_actions"] == ["follow"]


@pytest.mark.anyio
async def test_action_repair_rejects_action_hidden_by_turn_configuration() -> None:
    runtime = FakeFollowHandoffRuntime()
    recorder = EvaluationRecorder()
    prompt_provider = FixedPromptConfigProvider(_follow_prompt_snapshot())
    model_provider = FakeProvider(
        [
            '{"type":"query_inventory","args":{}}',
            '{"type":"follow","args":{"entity_id":72,"follow_distance":1.25}}',
        ]
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=model_provider),
        context_manager=ContextManager(  # type: ignore[arg-type]
            prompt_config_provider=prompt_provider
        ),
        tool_registry=ToolRegistry(["follow", "query_inventory"]),
        recorder=recorder,
        action_repair_policy=ActionRepairPolicy(
            ActionRepairConfig(max_attempts=1, model_timeout_retries=0)
        ),
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "reject_config_hidden_action",
        task_spec={"goal": "Follow the selected sheep."},
    )

    invalid_event = next(
        event for event in recorder.events if event.event_type == "invalid_action"
    )
    assert result.steps[0].action.type == "follow"
    assert runtime.actions == [
        HarnessAction(type="follow", args={"entity_id": 72, "follow_distance": 1.25})
    ]
    assert "not exposed by this turn" in invalid_event.payload["error"]
    assert model_provider.calls == 2
    assert prompt_provider.calls == 1


@pytest.mark.anyio
async def test_execution_loop_runs_one_audited_inventory_step() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    router = ModelRouter(provider=FakeProvider('{"type":"query_inventory","args":{}}'))
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "inspect_inventory",
        task_spec={"goal": "Check inventory before collecting log."},
        task_memory=[],
    )

    assert runtime.reset_spec is not None
    assert runtime.actions[0].type == "query_inventory"
    assert result.steps[0].action.type == "query_inventory"
    assert [event.event_type for event in recorder.events] == [
        "run_started",
        "observation",
        "context_built",
        "model_action",
        "action_result",
        "run_finished",
    ]
    model_event = next(event for event in recorder.events if event.event_type == "model_action")
    assert model_event.payload["raw_response"]["request_model"] == router.default_model
    assert model_event.payload["raw_response"]["response_model"] == router.default_model
    assert model_event.payload["decision"]["action"]["type"] == "query_inventory"
    assert model_event.payload["decision"]["knowledge_need"]["needed"] is False
    assert recorder.events[-1].payload["stop_reason"] == "max_steps_exhausted"


@pytest.mark.anyio
async def test_execution_loop_applies_memory_update_without_an_extra_model_call() -> None:
    runtime = FakeEntityMetadataRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            '{"type":"scan_entities","args":{"entity":"sheep"}}',
            (
                '{"reasoning_summary":"Remember the inspected sheep before continuing.",'
                '"evidence":["step 0 semantically decoded this sheep"],'
                '"memory_update":[{"memory_key":"entity:68/wool_state",'
                '"source_ref":"step:0/scan_entities/entity:68",'
                '"paths":["/entity_id","/details/metadata_decoded/wool/color",'
                '"/details/metadata_decoded/wool/is_sheared"],'
                '"note":"This sheep is brown and already sheared."}],'
                '"action":{"type":"query_inventory","args":{}}}'
            ),
        ]
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=provider),
        tool_registry=ToolRegistry(["scan_entities", "query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=2),
    )

    result = await loop.run(
        "find_white_sheep",
        task_spec={"goal": "Obtain one white wool."},
    )

    assert len(result.steps) == 2
    assert provider.calls == 2
    memory_event = next(
        event for event in recorder.events if event.event_type == "memory_update"
    )
    assert memory_event.payload["outcomes"][0]["accepted"] is True
    selected = memory_event.payload["outcomes"][0]["entry"]["selected_values"]
    assert selected[-1]["value"] is True


@pytest.mark.anyio
async def test_execution_loop_resolves_memory_from_same_response_action_result() -> None:
    runtime = FakeEntityMetadataRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        (
            '{"reasoning_summary":"Scan and retain the decoded target mismatch.",'
            '"evidence":["the current observation shows a nearby brown sheep"],'
            '"memory_update":[{"memory_key":"entity:68/wool_state",'
            '"source_ref":"step:0/scan_entities/entity:68",'
            '"paths":["/entity_id","/details/metadata_decoded/wool/color",'
            '"/details/metadata_decoded/wool/is_sheared"],'
            '"note":"This sheep is brown and already sheared."}],'
            '"action":{"type":"scan_entities","args":{"entity":"sheep"}}}'
        )
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=provider),
        tool_registry=ToolRegistry(["scan_entities"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "find_white_sheep",
        task_spec={"goal": "Obtain one white wool."},
    )

    assert len(result.steps) == 1
    assert provider.calls == 1
    memory_event = next(
        event for event in recorder.events if event.event_type == "memory_update"
    )
    assert memory_event.payload["outcomes"][0]["accepted"] is True
    selected = memory_event.payload["outcomes"][0]["entry"]["selected_values"]
    assert selected[0] == {"path": "/entity_id", "value": 68}
    assert selected[1] == {
        "path": "/details/metadata_decoded/wool/color",
        "value": "brown",
    }


@pytest.mark.anyio
async def test_execution_loop_waits_for_follow_handoff_before_entity_action() -> None:
    runtime = FakeFollowHandoffRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            '{"type":"follow","args":{"entity_id":72,"follow_distance":1.25}}',
            '{"type":"use_item","args":{"entity_id":72}}',
        ]
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=provider),
        tool_registry=ToolRegistry(["follow", "use_item"]),
        recorder=recorder,
        budget=ExecutionBudget(
            max_steps=2,
            follow_handoff_max_wait_sec=0.1,
            follow_handoff_poll_interval_sec=0.001,
        ),
    )

    result = await loop.run(
        "follow_then_shear",
        task_spec={"goal": "Use shears on the selected sheep."},
    )

    assert [step.action.type for step in result.steps] == ["follow", "use_item"]
    assert result.steps[-1].action_result["distance_at_dispatch"] == 1.5
    handoff_event = next(
        event for event in recorder.events if event.event_type == "follow_handoff_wait"
    )
    assert handoff_event.payload["status"] == "ready"
    assert handoff_event.payload["target"]["entity_id"] == 72
    assert handoff_event.payload["initial_distance"] == 8.0
    assert handoff_event.payload["final_distance"] == 1.5
    assert handoff_event.payload["poll_count"] == 2


@pytest.mark.anyio
async def test_execution_loop_accepts_unverified_finish_without_an_evaluator() -> None:
    """Termination remains available without falsely claiming unverified task success."""

    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(
            provider=FakeProvider('{"type":"submit_for_evaluation","args":{}}')
        ),
        tool_registry=ToolRegistry(["query_inventory", "submit_for_evaluation"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "unverified_demo",
        task_spec={"goal": "Inspect inventory without a task evaluator."},
        task_memory=[],
    )

    context_event = next(event for event in recorder.events if event.event_type == "context_built")
    action_contract = context_event.payload["prompt_sections"]["stable_system_payload"][
        "action_contract"
    ]
    termination_contract = context_event.payload["prompt_sections"]["stable_system_payload"][
        "termination_contract"
    ]
    assert runtime.actions == []
    assert result.terminated is True
    assert result.stop_reason == "agent_finished_unverified"
    assert result.steps[0].action_result["task_success"] is None
    assert result.steps[0].action_result["evaluation_status"] == "not_evaluated"
    assert "submit_for_evaluation" in action_contract["allowed_actions"]
    assert termination_contract["finish_enabled"] is True
    assert termination_contract["without_evaluator"] == "terminate_as_unverified"


@pytest.mark.anyio
async def test_execution_loop_submits_creative_task_to_external_evaluator() -> None:
    """A creative finish request should stop acting without claiming task success."""

    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    router = ModelRouter(
        provider=FakeProvider(
            """{
              "reasoning_summary":"The current visual and state evidence support submission.",
              "evidence":["The target scene is present in the current observation."],
              "knowledge_need":{"needed":false,"query":null,"reason":null},
              "action":{"type":"submit_for_evaluation","args":{}}
            }"""
        )
    )

    async def creative_checker(
        task_spec: dict[str, Any],
        steps: list[Any],
    ) -> dict[str, Any]:
        """Require the configured external MineCLIP evaluator."""

        _ = (task_spec, steps)
        return {
            "success": False,
            "inconclusive": True,
            "reason": "MineCLIP must score the completed recording.",
            "checks": [{"type": "creative_mineclip", "external_evaluator": "mineclip"}],
        }

    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["submit_for_evaluation"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=5),
        success_checker=creative_checker,
    )

    result = await loop.run(
        "creative_scene",
        task_spec={
            "goal": "Build a visible scene.",
            "verifier": {"type": "creative_mineclip"},
        },
        task_memory=[],
    )

    event_types = [event.event_type for event in recorder.events]
    submission_result = result.steps[0].action_result
    assert runtime.actions == []
    assert result.terminated is True
    assert result.stop_reason == "agent_submitted_for_external_evaluation"
    assert submission_result["ok"] is True
    assert submission_result["submission_accepted"] is True
    assert submission_result["task_success"] is None
    assert "agent_finish_requested" in event_types
    assert "agent_finish_accepted" in event_types


@pytest.mark.anyio
async def test_execution_loop_rejects_premature_finish_and_continues_acting() -> None:
    """A definitive verifier failure should return evidence to the next ReAct turn."""

    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            '{"reasoning_summary":"Try submission.","evidence":[],"action":{"type":"submit_for_evaluation","args":{}}}',
            '{"reasoning_summary":"The verifier rejected submission, so inspect inventory.","evidence":["The previous verifier did not confirm the goal."],"action":{"type":"query_inventory","args":{}}}',
        ]
    )

    async def programmatic_checker(
        task_spec: dict[str, Any],
        steps: list[Any],
    ) -> dict[str, Any]:
        """Confirm success only after the non-control action executes."""

        _ = task_spec
        success = bool(steps and steps[-1].action.type == "query_inventory")
        return {
            "success": success,
            "reason": "Goal confirmed." if success else "Required evidence is still missing.",
            "checks": [],
        }

    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=provider),
        tool_registry=ToolRegistry(["query_inventory", "submit_for_evaluation"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=2),
        success_checker=programmatic_checker,
    )

    result = await loop.run(
        "programmatic_finish_guard",
        task_spec={"goal": "Confirm inventory.", "verifier": {"type": "inventory_contains"}},
        task_memory=[],
    )

    event_types = [event.event_type for event in recorder.events]
    assert runtime.actions == [HarnessAction(type="query_inventory", args={})]
    assert result.stop_reason == "success_checker"
    assert result.steps[0].action_result["error_code"] == "submission_rejected"
    assert result.steps[0].action_result["recoverable"] is True
    assert "agent_finish_rejected" in event_types


@pytest.mark.anyio
async def test_execution_loop_accepts_finish_after_online_verification() -> None:
    """An online verifier may accept a finish request without dispatching a runtime action."""

    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    router = ModelRouter(
        provider=FakeProvider(
            '{"reasoning_summary":"The inventory already satisfies the goal.","evidence":["Current inventory contains the target."],"action":{"type":"submit_for_evaluation","args":{}}}'
        )
    )

    async def successful_checker(
        task_spec: dict[str, Any],
        steps: list[Any],
    ) -> dict[str, Any]:
        """Return a deterministic successful verifier result."""

        _ = (task_spec, steps)
        return {"success": True, "reason": "Target is present.", "checks": []}

    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["submit_for_evaluation"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=2),
        success_checker=successful_checker,
    )

    result = await loop.run(
        "verified_finish",
        task_spec={"goal": "Possess the target.", "verifier": {"type": "inventory_contains"}},
        task_memory=[],
    )

    assert runtime.actions == []
    assert result.terminated is True
    assert result.stop_reason == "agent_submitted_verified"
    assert result.steps[0].action_result["evaluation_status"] == "verified_success"


@pytest.mark.anyio
async def test_execution_loop_injects_initial_visual_snapshot_into_first_model_turn(
    tmp_path: Path,
) -> None:
    """Creative runs should send one trusted baseline frame before relying on model initiative."""

    frame = tmp_path / "initial.jpg"
    frame.write_bytes(b"bounded-test-frame")
    runtime = FakeInitialVisualRuntime(str(frame))
    recorder = EvaluationRecorder()
    router = ModelRouter(provider=FakeProvider('{"type":"query_inventory","args":{}}'))
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["request_visual_snapshot", "query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "creative_visual_smoke",
        task_spec={"goal": "Build something visible.", "initial_visual_snapshot": True},
    )

    assert [action.type for action in runtime.actions] == [
        "request_visual_snapshot",
        "query_inventory",
    ]
    assert len(result.steps) == 1
    initial_event = next(
        event for event in recorder.events if event.event_type == "initial_visual_snapshot"
    )
    assert initial_event.payload["ok"] is True
    context_event = next(event for event in recorder.events if event.event_type == "context_built")
    assert context_event.payload["prompt_sections"]["visual_input"]["artifact_path"] == str(frame)
    model_event = next(event for event in recorder.events if event.event_type == "model_action")
    assert model_event.payload["raw_response"]["request_vision_input"] is True


@pytest.mark.anyio
async def test_execution_loop_stops_attempt_on_unknown_worker_action_state() -> None:
    """A responsive health probe must not allow overlapping actions after an RPC timeout."""

    runtime = FakeRpcTimeoutRuntime()
    recorder = EvaluationRecorder()
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=FakeProvider('{"type":"query_inventory","args":{}}')),
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=3),
    )

    result = await loop.run("rpc_timeout", task_spec={"goal": "Inspect inventory."})

    assert result.terminated is True
    assert result.stop_reason == "action_rpc_timeout"
    assert len(result.steps) == 1
    timeout_event = next(
        event for event in recorder.events if event.event_type == "runtime_action_timeout"
    )
    assert timeout_event.payload["worker_health"]["responsive"] is True


@pytest.mark.anyio
async def test_execution_loop_dispatches_knowledge_tool_without_runtime_action() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    router = ModelRouter(
        provider=FakeProvider(
            '{"type":"retrieve_docs","args":{"query":"oak log mining","limit":1}}'
        )
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["retrieve_docs"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "lookup_knowledge",
        task_spec={"goal": "Find how to harvest oak logs."},
        task_memory=[],
    )

    assert runtime.actions == []
    assert result.steps[0].action.type == "retrieve_docs"
    assert result.steps[0].action_result["ok"] is True
    assert result.steps[0].action_result["docs"]
    event_types = [event.event_type for event in recorder.events]
    assert "knowledge_tool_call" in event_types
    assert "action_result" in event_types


@pytest.mark.anyio
async def test_execution_loop_reuses_identical_knowledge_query_with_audited_cache_hit() -> None:
    """Repeated deterministic reads should not hit the provider twice within one run."""

    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            '{"type":"retrieve_docs","args":{"query":"oak log mining","limit":1}}',
            '{"type":"query_inventory","args":{}}',
            '{"type":"retrieve_docs","args":{"query":"oak log mining","limit":1}}',
        ]
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=ModelRouter(provider=provider),
        tool_registry=ToolRegistry(["retrieve_docs", "query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=3),
    )

    result = await loop.run(
        "cached_knowledge",
        task_spec={"goal": "Find and harvest an oak log."},
        task_memory=[],
    )

    knowledge_events = [
        event for event in recorder.events if event.event_type == "knowledge_tool_call"
    ]
    contexts = [event for event in recorder.events if event.event_type == "context_built"]
    assert len(result.steps) == 3
    assert len(runtime.actions) == 1
    assert knowledge_events[0].payload["result"].get("cache_hit") is None
    assert knowledge_events[1].payload["result"]["cache_hit"] is True
    assert (
        contexts[1].payload["prompt_sections"]["user_payload"]["run_context"]["knowledge"][
            "entries"
        ]
        == []
    )
    assert contexts[2].payload["prompt_sections"]["user_payload"]["run_context"]["knowledge"][
        "entries"
    ]


@pytest.mark.anyio
async def test_execution_loop_stops_after_success_checker_passes() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            '{"type":"query_inventory","args":{}}',
            '{"type":"query_inventory","args":{}}',
        ]
    )
    router = ModelRouter(provider=provider)

    async def success_checker(
        task_spec: dict[str, Any],
        steps: list[Any],
    ) -> dict[str, Any]:
        """Return success after the first completed audited step."""

        _ = task_spec
        return {"success": bool(steps), "reason": "done", "checks": []}

    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=3),
        success_checker=success_checker,
    )

    result = await loop.run(
        "inspect_inventory",
        task_spec={"goal": "Check inventory before collecting log."},
        task_memory=[],
    )

    assert result.terminated is True
    assert len(result.steps) == 1
    assert provider.calls == 1
    assert len(runtime.actions) == 1
    verifier_event = next(
        event for event in recorder.events if event.event_type == "step_verifier_result"
    )
    assert verifier_event.payload["success"] is True
    finish_event = recorder.events[-1]
    assert finish_event.event_type == "run_finished"
    assert finish_event.payload["stop_reason"] == "success_checker"


@pytest.mark.anyio
async def test_execution_loop_records_reachability_analysis_for_move_to() -> None:
    runtime = FakeReachabilityRuntime()
    recorder = EvaluationRecorder()
    router = ModelRouter(
        provider=FakeProvider('{"type":"move_to","args":{"position":{"x":3,"y":68,"z":4}}}')
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["move_to"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    await loop.run(
        "reachability_probe",
        task_spec={
            "runtime": {"username": "HarnessTrainer1"},
            "training": {"worker_id": "worker-1"},
        },
        task_memory=[],
    )

    event = next(event for event in recorder.events if event.event_type == "reachability_analysis")
    payload = event.payload
    assert payload["step_index"] == 0
    assert payload["agent_id"] == "HarnessTrainer1"
    assert payload["error_code"] == "no_path"
    assert payload["nearest_reachable_position"] == {"x": 3, "y": 65, "z": 4}
    assert payload["target_height_delta"] == 3
    assert payload["path_summary"]["status"] == "partial"
    assert payload["path_summary"]["last_node"] == {"x": 3, "y": 65, "z": 4}


@pytest.mark.anyio
async def test_execution_loop_records_agent_plan_when_planner_is_enabled() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            (
                '{"goal":"Collect one log","known_targets":[{"id":"oak_log","kind":"block",'
                '"evidence":"task goal"}],"knowledge_used":[],"retrieved_skills":[],'
                '"high_level_strategy":"Find a current-world target from observation, then act.",'
                '"current_phase":"locate_target","open_questions":[],"recovery_policy":[]}'
            ),
            '{"type":"query_inventory","args":{}}',
        ]
    )
    router = ModelRouter(provider=provider)
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        planner=AgentPlanner(),
        budget=ExecutionBudget(max_steps=1),
    )

    await loop.run(
        "planned_task",
        task_spec={"goal": "Collect one log.", "allowed_actions": ["query_inventory"]},
        task_memory=[],
    )

    event_types = [event.event_type for event in recorder.events]
    assert "agent_plan_model_call" in event_types
    assert "agent_plan_created" in event_types
    context_event = next(event for event in recorder.events if event.event_type == "context_built")
    user_payload = context_event.payload["prompt_sections"]["user_payload"]
    assert user_payload["task_plan"]["current_phase"] == "locate_target"
    assert user_payload["task_plan"]["semantics"] == "contextual_guidance_not_macro_execution"


@pytest.mark.anyio
async def test_execution_loop_revises_plan_after_navigation_failure() -> None:
    runtime = FakeReachabilityRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        [
            (
                '{"goal":"Reach target","known_targets":[],"knowledge_used":[],'
                '"retrieved_skills":[],"high_level_strategy":"Move to the target.",'
                '"current_phase":"approach","open_questions":[],"recovery_policy":[]}'
            ),
            '{"type":"move_to","args":{"position":{"x":3,"y":68,"z":4}}}',
            (
                '{"goal":"Reach target","known_targets":[],"knowledge_used":[],'
                '"retrieved_skills":[],"high_level_strategy":"Use the nearest reachable point, then re-scan.",'
                '"current_phase":"recover_navigation","open_questions":[],"recovery_policy":["use nearest reachable point"]}'
            ),
        ]
    )
    router = ModelRouter(provider=provider)
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["move_to"]),
        recorder=recorder,
        planner=AgentPlanner(),
        budget=ExecutionBudget(max_steps=1),
    )

    await loop.run(
        "planned_reachability_probe",
        task_spec={"goal": "Move to a difficult target.", "allowed_actions": ["move_to"]},
        task_memory=[],
    )

    revised = next(event for event in recorder.events if event.event_type == "agent_plan_revised")
    assert revised.payload["plan"]["current_phase"] == "recover_navigation"
    assert revised.payload["plan"]["revision"] == 1


@pytest.mark.anyio
async def test_execution_loop_falls_back_when_action_stays_outside_task_scope() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(
        '{"type":"dig_block_at","args":{"block":"oak_log","position":{"x":0,"y":64,"z":0}}}'
    )
    router = ModelRouter(provider=provider)
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    await loop.run(
        "restricted_task",
        task_spec={"goal": "Try to mine log.", "allowed_actions": ["query_inventory"]},
        task_memory=[],
    )

    event_types = [event.event_type for event in recorder.events]
    assert provider.calls == 2
    assert runtime.actions == [HarnessAction(type="query_inventory", args={})]
    assert "invalid_action" in event_types
    assert "model_repair_attempt" in event_types
    assert "model_fallback_action" in event_types


@pytest.mark.anyio
async def test_execution_loop_dispatches_simple_dig_attempt_when_allowed() -> None:
    runtime = FakeRuntime()
    router = ModelRouter(
        provider=FakeProvider(
            '{"type":"dig_block_at","args":{"block":"oak_log","position":{"x":0,"y":64,"z":0}}}'
        )
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["dig_block_at"]),
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "dig_nearby_log",
        task_spec={"goal": "Dig one nearby oak log.", "allowed_actions": ["dig_block_at"]},
        task_memory=[],
    )

    expected_args = {"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}}
    assert runtime.actions == [HarnessAction(type="dig_block_at", args=expected_args)]
    assert result.steps[0].action.args == expected_args


@pytest.mark.anyio
async def test_execution_loop_repairs_invalid_json_before_dispatch() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = FakeProvider(["bot.chat('bad')", '{"type":"query_inventory","args":{}}'])
    router = ModelRouter(provider=provider)
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        budget=ExecutionBudget(max_steps=1),
    )

    await loop.run(
        "repair_bad_json",
        task_spec={"goal": "Check inventory.", "allowed_actions": ["query_inventory"]},
        task_memory=[],
    )

    event_types = [event.event_type for event in recorder.events]
    assert provider.calls == 2
    assert runtime.actions == [HarnessAction(type="query_inventory", args={})]
    assert "model_error" in event_types
    assert "model_repair_attempt" in event_types
    assert "model_repair_success" in event_types


@pytest.mark.anyio
async def test_execution_loop_retries_model_timeout_without_advancing_step() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = TimeoutThenSuccessProvider(
        timeout_count=1,
        action_json='{"type":"query_inventory","args":{}}',
    )
    router = ModelRouter(provider=provider)
    repair_policy = ActionRepairPolicy(
        ActionRepairConfig(
            max_attempts=0,
            model_timeout_retries=1,
            model_timeout_backoff_sec=(0.0,),
        )
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        action_repair_policy=repair_policy,
        budget=ExecutionBudget(max_steps=1),
    )

    result = await loop.run(
        "retry_model_timeout",
        task_spec={"goal": "Check inventory.", "allowed_actions": ["query_inventory"]},
        task_memory=[],
    )

    event_types = [event.event_type for event in recorder.events]
    assert provider.calls == 2
    assert len(result.steps) == 1
    assert runtime.actions == [HarnessAction(type="query_inventory", args={})]
    assert "model_timeout" in event_types
    assert "model_timeout_retry" in event_types
    assert "model_action" in event_types


@pytest.mark.anyio
async def test_execution_loop_raises_model_timeout_after_retry_budget() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    provider = TimeoutThenSuccessProvider(
        timeout_count=3,
        action_json='{"type":"query_inventory","args":{}}',
    )
    router = ModelRouter(provider=provider)
    repair_policy = ActionRepairPolicy(
        ActionRepairConfig(
            max_attempts=0,
            model_timeout_retries=1,
            model_timeout_backoff_sec=(0.0,),
        )
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        action_repair_policy=repair_policy,
        budget=ExecutionBudget(max_steps=1),
    )

    with pytest.raises(ActionGenerationTimeout):
        await loop.run(
            "exhaust_model_timeout",
            task_spec={"goal": "Check inventory.", "allowed_actions": ["query_inventory"]},
            task_memory=[],
        )

    event_types = [event.event_type for event in recorder.events]
    assert provider.calls == 2
    assert runtime.actions == []
    assert event_types.count("model_timeout") == 2
    assert "model_timeout_exhausted" in event_types


@pytest.mark.anyio
async def test_execution_loop_raises_when_repair_and_fallback_are_unavailable() -> None:
    runtime = FakeRuntime()
    recorder = EvaluationRecorder()
    router = ModelRouter(provider=FakeProvider("bot.chat('bad')"))
    repair_policy = ActionRepairPolicy(
        ActionRepairConfig(
            max_attempts=0,
            fallback_actions=(HarnessAction(type="query_inventory", args={}),),
        )
    )
    loop = ExecutionLoop(
        runtime=runtime,
        model_router=router,
        tool_registry=ToolRegistry(["dig_block_at"]),
        recorder=recorder,
        action_repair_policy=repair_policy,
        budget=ExecutionBudget(max_steps=1),
    )

    with pytest.raises(ActionRepairFailed):
        await loop.run(
            "no_safe_fallback",
            task_spec={"goal": "Dig a nearby log.", "allowed_actions": ["dig_block_at"]},
            task_memory=[],
        )

    assert runtime.actions == []
    assert "model_repair_exhausted" in [event.event_type for event in recorder.events]
