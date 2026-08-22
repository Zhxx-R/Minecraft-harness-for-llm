from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mc_agent_harness.models.router import ModelRouter, ModelRouterError, ModelUsage
from mc_agent_harness.schemas.action import HarnessAction


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """Agent-created task plan used as contextual guidance, not a fixed workflow."""

    goal: str
    known_targets: list[dict[str, Any]] = field(default_factory=list)
    knowledge_used: list[dict[str, Any]] = field(default_factory=list)
    retrieved_skills: list[dict[str, Any]] = field(default_factory=list)
    high_level_strategy: str = ""
    current_phase: str = "initial_assessment"
    open_questions: list[str] = field(default_factory=list)
    recovery_policy: list[str] = field(default_factory=list)
    source: str = "agent"
    revision: int = 0

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-safe representation for prompts and audit events."""

        return {
            "goal": self.goal,
            "known_targets": self.known_targets,
            "knowledge_used": self.knowledge_used,
            "retrieved_skills": self.retrieved_skills,
            "high_level_strategy": self.high_level_strategy,
            "current_phase": self.current_phase,
            "open_questions": self.open_questions,
            "recovery_policy": self.recovery_policy,
            "source": self.source,
            "revision": self.revision,
            "semantics": "contextual_guidance_not_macro_execution",
        }


@dataclass(frozen=True, slots=True)
class AgentPlanResult:
    """Planner output plus model-call metadata for persistence."""

    plan: TaskPlan
    raw_content: str
    usage: ModelUsage
    raw_response: dict[str, Any]
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AgentPlannerPolicy:
    """Limits controlling how often the agent may revise its own plan."""

    max_revisions: int = 3


class AgentPlanner:
    """Creates and revises task plans through the configured LLM."""

    def __init__(self, policy: AgentPlannerPolicy | None = None) -> None:
        self.policy = policy or AgentPlannerPolicy()

    async def create_plan(
        self,
        *,
        model_router: ModelRouter,
        task_spec: dict[str, Any],
        observation: dict[str, Any],
        task_memory: list[str],
        allowed_actions: list[str],
    ) -> AgentPlanResult:
        """Ask the model to create the initial run plan from non-oracle context."""

        messages = _planner_messages(
            task_spec=task_spec,
            observation=observation,
            task_memory=task_memory,
            allowed_actions=allowed_actions,
            current_plan=None,
            previous_step=None,
            revision=0,
        )
        return await self._generate_plan(model_router, messages, task_spec, revision=0)

    async def revise_plan(
        self,
        *,
        model_router: ModelRouter,
        task_spec: dict[str, Any],
        observation: dict[str, Any],
        task_memory: list[str],
        allowed_actions: list[str],
        current_plan: TaskPlan,
        previous_step: dict[str, Any],
    ) -> AgentPlanResult:
        """Ask the model to revise a plan after runtime evidence contradicts it."""

        revision = current_plan.revision + 1
        messages = _planner_messages(
            task_spec=task_spec,
            observation=observation,
            task_memory=task_memory,
            allowed_actions=allowed_actions,
            current_plan=current_plan.to_json(),
            previous_step=previous_step,
            revision=revision,
        )
        return await self._generate_plan(model_router, messages, task_spec, revision=revision)

    def should_revise(
        self,
        *,
        action: HarnessAction,
        action_result: dict[str, Any],
        revision_count: int,
    ) -> bool:
        """Return whether the latest evidence should trigger a bounded plan revision."""

        if revision_count >= self.policy.max_revisions:
            return False
        error_code = str(action_result.get("error_code") or action_result.get("status") or "")
        if action.type == "move_to" and error_code in {"timeout", "no_path", "path_timeout", "path_stopped"}:
            return True
        if action.type in {"move_to_and_engage_combat", "engage_combat"} and error_code in {
            "target_unreachable",
            "target_lost",
            "no_line_of_sight",
            "low_health",
            "no_ammo",
        }:
            return True
        if action.type == "scan_entities" and _empty_scan(action_result, "entities"):
            return True
        if action.type == "scan_blocks" and _empty_scan(action_result, "blocks"):
            return True
        return False

    async def _generate_plan(
        self,
        model_router: ModelRouter,
        messages: list[dict[str, Any]],
        task_spec: dict[str, Any],
        *,
        revision: int,
    ) -> AgentPlanResult:
        """Generate a plan and fall back to a minimal auditable plan on parser errors."""

        try:
            result = await model_router.generate_json(messages)
        except ModelRouterError as exc:
            plan = _fallback_plan(task_spec, revision=revision, reason=str(exc))
            return AgentPlanResult(
                plan=plan,
                raw_content=exc.raw_content or "",
                usage=exc.usage,
                raw_response=exc.raw_response,
                fallback_reason=str(exc),
            )
        plan = _plan_from_payload(result.payload, task_spec, revision=revision)
        return AgentPlanResult(
            plan=plan,
            raw_content=result.raw_content,
            usage=result.usage,
            raw_response=result.raw_response,
        )


def _planner_messages(
    *,
    task_spec: dict[str, Any],
    observation: dict[str, Any],
    task_memory: list[str],
    allowed_actions: list[str],
    current_plan: dict[str, Any] | None,
    previous_step: dict[str, Any] | None,
    revision: int,
) -> list[dict[str, Any]]:
    """Build the planner prompt without embedding task-side solution steps."""

    system = (
        "You are the planning module of a Minecraft agent harness. Create a concise JSON "
        "TaskPlan that guides future ReAct decisions without prescribing fixed coordinates or "
        "a hard-coded action macro. Use only task goal, current observation, prior memory, "
        "retrieved skill summaries if present, and allowed knowledge/action tools. If knowledge "
        "is missing, put the needed lookup in open_questions instead of inventing facts."
    )
    payload = {
        "revision": revision,
        "task": _planner_task_payload(task_spec),
        "observation_summary": _planner_observation_payload(observation),
        "task_memory": task_memory[-5:],
        "allowed_actions": allowed_actions,
        "current_plan": current_plan,
        "previous_step": previous_step,
        "output_schema": {
            "goal": "string",
            "known_targets": [{"id": "string", "kind": "item|block|entity|unknown", "evidence": "string"}],
            "knowledge_used": [{"tool": "string", "summary": "string"}],
            "retrieved_skills": [{"name": "string", "summary": "string"}],
            "high_level_strategy": "string",
            "current_phase": "string",
            "open_questions": ["string"],
            "recovery_policy": ["string"],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _json_dumps(payload)},
    ]


def _planner_task_payload(task_spec: dict[str, Any]) -> dict[str, Any]:
    """Expose task target and verifier metadata while excluding oracle-only fields."""

    hidden = {"runtime", "training", "start_delay_sec", "_initial_inventory"}
    payload = {key: value for key, value in task_spec.items() if key not in hidden}
    benchmark = payload.get("benchmark")
    if isinstance(benchmark, dict):
        payload["benchmark"] = {
            key: value
            for key, value in benchmark.items()
            if key not in {"scripted_actions", "initial_state"}
        }
    return payload


def _planner_observation_payload(observation: dict[str, Any]) -> dict[str, Any]:
    """Keep planner observation compact and stable across worker versions."""

    return {
        "position": observation.get("position"),
        "health": observation.get("health"),
        "food": observation.get("food"),
        "inventory": observation.get("inventory", [])[:24] if isinstance(observation.get("inventory"), list) else [],
        "equipment": observation.get("equipment"),
        "nearby_entities": observation.get("nearby_entities", [])[:10]
        if isinstance(observation.get("nearby_entities"), list)
        else [],
        "nearby_hostile_entities": observation.get("nearby_hostile_entities", [])[:10]
        if isinstance(observation.get("nearby_hostile_entities"), list)
        else [],
        "nearby_blocks": observation.get("nearby_blocks", [])[:16]
        if isinstance(observation.get("nearby_blocks"), list)
        else [],
    }


def _plan_from_payload(payload: dict[str, Any], task_spec: dict[str, Any], *, revision: int) -> TaskPlan:
    """Normalize model JSON into a stable TaskPlan object."""

    return TaskPlan(
        goal=str(payload.get("goal") or task_spec.get("goal") or task_spec.get("task_id") or ""),
        known_targets=_list_of_dicts(payload.get("known_targets")),
        knowledge_used=_list_of_dicts(payload.get("knowledge_used")),
        retrieved_skills=_list_of_dicts(payload.get("retrieved_skills")),
        high_level_strategy=str(payload.get("high_level_strategy") or ""),
        current_phase=str(payload.get("current_phase") or "initial_assessment"),
        open_questions=[str(item) for item in _list(payload.get("open_questions"))],
        recovery_policy=[str(item) for item in _list(payload.get("recovery_policy"))],
        revision=revision,
    )


def _fallback_plan(task_spec: dict[str, Any], *, revision: int, reason: str) -> TaskPlan:
    """Create a minimal plan when the model cannot produce valid planner JSON."""

    return TaskPlan(
        goal=str(task_spec.get("goal") or task_spec.get("task_id") or ""),
        high_level_strategy="Continue with ReAct: observe, use knowledge tools when terms are unclear, act from evidence, and verify progress.",
        current_phase="fallback_after_planner_error",
        open_questions=["Planner output was invalid; use knowledge tools for uncertain Minecraft terms."],
        recovery_policy=[reason],
        source="fallback",
        revision=revision,
    )


def _empty_scan(action_result: dict[str, Any], field: str) -> bool:
    """Detect empty scan results in both raw runtime and wrapped action payloads."""

    value = action_result.get(field)
    if isinstance(value, list):
        return len(value) == 0
    nested = action_result.get("result")
    if isinstance(nested, dict) and isinstance(nested.get(field), list):
        return len(nested[field]) == 0
    return False


def _list(value: Any) -> list[Any]:
    """Coerce an unknown JSON value into a list."""

    return value if isinstance(value, list) else []


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    """Coerce unknown model JSON into a list of dictionaries."""

    return [item for item in _list(value) if isinstance(item, dict)]


def _json_dumps(payload: dict[str, Any]) -> str:
    """Serialize planner context without non-ASCII assumptions."""

    import json

    return json.dumps(payload, ensure_ascii=True, sort_keys=True)
