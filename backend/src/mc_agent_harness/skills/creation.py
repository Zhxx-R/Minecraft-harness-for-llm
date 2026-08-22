from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mc_agent_harness.db.models import RunRecord, StepRecord
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.schemas.skill import SkillStatus


REUSABLE_WORKFLOW_ACTIONS = frozenset(
    {
        "scan_blocks",
        "scan_dropped_items",
        "move_to",
        "follow",
        "dig_block_at",
        "wait_ticks",
        "process_item",
        "place_block",
        "smelt_item",
        "equip_item",
        "scan_entities",
        "move_to_and_engage_combat",
        "engage_combat",
        "consume_item",
        "fight_entity",
        "use_item",
    }
)
KNOWLEDGE_COVERED_ACTIONS = frozenset({"craft_item"})
# Entity-targeted actions that can be filtered against an explicit combat verifier.
ENGAGEMENT_ACTIONS = frozenset({"move_to_and_engage_combat", "engage_combat"})
ENTITY_TARGET_ACTIONS = frozenset({"scan_entities", "follow", "fight_entity", *ENGAGEMENT_ACTIONS})
PROGRAMMATIC_SKILL_NAME_PREFIXES = {
    "combat": "defeat",
    "harvest": "harvest",
    "survival": "survival",
    "techtree": "techtree",
}


@dataclass(frozen=True, slots=True)
class SkillCreationDecision:
    """Policy decision explaining whether a successful trajectory should become a skill."""

    should_create: bool
    reason: str
    status: SkillStatus = SkillStatus.draft
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """Deterministic skill fields produced from a source trajectory."""

    name: str
    description: str
    strategy_summary: str
    triggers: list[str]
    preconditions: list[str]
    task_scope: list[str]
    dependencies: list[str]
    parameterized_plan: list[dict[str, Any]]
    recovery_policy: list[str]
    source_evidence: dict[str, Any]
    verifier_stats: dict[str, Any]
    validation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillEvidenceSelection:
    """Task-relevant trajectory steps plus auditable exclusions."""

    steps: list[StepRecord]
    excluded_steps: list[dict[str, Any]] = field(default_factory=list)
    verifier_entity_target: str | None = None


class SkillCreationPolicy:
    """Decide when a successful run is worth turning into a reusable skill."""

    def evaluate(
        self,
        run: RunRecord,
        all_steps: list[StepRecord],
        successful_steps: list[StepRecord],
    ) -> SkillCreationDecision:
        """Apply reusable-workflow gates before candidate creation."""

        action_types = _action_types(successful_steps)
        failed_before_success = any(step.action_result.get("ok") is False for step in all_steps)
        target_ids = _target_ids(run, successful_steps)
        evidence = {
            "action_types": action_types,
            "successful_step_count": len(successful_steps),
            "failed_before_success": failed_before_success,
            "target_ids": sorted(target_ids),
        }
        if not successful_steps:
            return SkillCreationDecision(False, "no_promotable_progress_action", evidence=evidence)
        if _is_simple_recipe_trace(run, successful_steps):
            return SkillCreationDecision(False, "covered_by_recipe_knowledge", evidence=evidence)
        if len(successful_steps) == 1 and not failed_before_success:
            return SkillCreationDecision(False, "single_step_without_recovery", evidence=evidence)
        if REUSABLE_WORKFLOW_ACTIONS & set(action_types):
            reason = "reusable_workflow"
            if failed_before_success:
                reason = "recovered_failure_workflow"
            return SkillCreationDecision(True, reason, evidence=evidence)
        return SkillCreationDecision(False, "low_reuse_signal", evidence=evidence)


class SkillSummarizer:
    """Summarize a successful trajectory into reviewable procedural skill metadata."""

    def summarize(
        self,
        run: RunRecord,
        action_plan: list[HarnessAction],
        successful_steps: list[StepRecord],
        decision: SkillCreationDecision,
        excluded_steps: list[dict[str, Any]] | None = None,
    ) -> SkillSummary:
        """Build deterministic metadata and a parameterized plan for a candidate skill."""

        target_ids = _target_ids(run, successful_steps)
        primary_target = _primary_target(run, action_plan, target_ids)
        name = derive_skill_name(run, primary_target)
        parameterized_plan = _parameterized_plan(action_plan, primary_target)
        recovery_policy = _recovery_policy(action_plan, primary_target)
        source_evidence = _source_evidence(
            run,
            successful_steps,
            action_plan,
            primary_target,
            excluded_steps=excluded_steps or [],
        )
        verifier_stats = _verifier_stats(run)
        validation = {
            "source_run_status": run.status,
            "source_step_count": len(successful_steps),
            "candidate_policy": decision.reason,
            "policy_evidence": decision.evidence,
            "parameterized_plan": parameterized_plan,
            "action_plan_semantics": "source_replay_only_not_default_macro_execution",
            "excluded_source_step_count": len(excluded_steps or []),
        }
        return SkillSummary(
            name=name,
            description=_description(run, primary_target, action_plan, decision.reason),
            strategy_summary=_strategy_summary(run, primary_target, action_plan, decision.reason),
            triggers=_triggers(run, action_plan, target_ids, primary_target),
            preconditions=_preconditions(successful_steps, action_plan, primary_target),
            task_scope=_task_scope(run, action_plan, target_ids),
            dependencies=_dependencies(action_plan, target_ids),
            parameterized_plan=parameterized_plan,
            recovery_policy=recovery_policy,
            source_evidence=source_evidence,
            verifier_stats=verifier_stats,
            validation=validation,
        )


def _is_simple_recipe_trace(run: RunRecord, successful_steps: list[StepRecord]) -> bool:
    """Return true for single-step crafting traces that should stay knowledge-backed."""

    if len(successful_steps) != 1:
        return False
    action = successful_steps[0].action if isinstance(successful_steps[0].action, dict) else {}
    if action.get("type") not in KNOWLEDGE_COVERED_ACTIONS:
        return False
    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    category = str(task_spec.get("category") or "").lower()
    return category in {"techtree", "craft", "crafting"} or bool(action.get("args", {}).get("item"))


def _action_types(steps: list[StepRecord]) -> list[str]:
    """Extract successful action type names in trajectory order."""

    action_types: list[str] = []
    for step in steps:
        if isinstance(step.action, dict) and step.action.get("type"):
            action_types.append(str(step.action["type"]))
    return action_types


def _target_ids(run: RunRecord, steps: list[StepRecord]) -> set[str]:
    """Collect canonical target ids from task metadata, actions, and action results."""

    targets: set[str] = set()
    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    _collect_target_ids(task_spec.get("verifier"), targets)
    _collect_target_ids(task_spec.get("success_criteria"), targets)
    for tag in task_spec.get("knowledge_tags", []):
        if isinstance(tag, str) and "/" in tag:
            targets.add(tag.rsplit("/", 1)[-1])
    for step in steps:
        action = step.action if isinstance(step.action, dict) else {}
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        for key in (
            "item",
            "item_id",
            "output",
            "input",
            "fuel",
            "block",
            "block_id",
            "entity",
            "entity_id",
            "station",
        ):
            value = args.get(key)
            if isinstance(value, str) and value:
                targets.add(value)
        result = step.action_result if isinstance(step.action_result, dict) else {}
        for key in ("item", "block", "entity", "query"):
            value = result.get(key)
            if isinstance(value, str) and value:
                targets.add(value)
    return {target for target in targets if target and target != "air"}


def select_relevant_skill_steps(
    run: RunRecord,
    steps: list[StepRecord],
) -> SkillEvidenceSelection:
    """Exclude entity actions that cannot contribute to an entity verifier target."""

    verifier_entity_target = _verifier_entity_target(run)
    if verifier_entity_target is None:
        return SkillEvidenceSelection(steps=list(steps))

    selected: list[StepRecord] = []
    excluded: list[dict[str, Any]] = []
    for step in steps:
        action = step.action if isinstance(step.action, dict) else {}
        action_type = str(action.get("type") or "")
        entity_target = _step_entity_target(step)
        if (
            action_type in ENTITY_TARGET_ACTIONS
            and entity_target is not None
            and not _same_entity_target(entity_target, verifier_entity_target)
        ):
            excluded.append(
                {
                    "step_index": int(step.step_index),
                    "action_type": action_type,
                    "observed_target": entity_target,
                    "verifier_target": verifier_entity_target,
                    "reason": "entity_target_mismatch",
                }
            )
            continue
        selected.append(step)
    return SkillEvidenceSelection(
        steps=selected,
        excluded_steps=excluded,
        verifier_entity_target=verifier_entity_target,
    )


def _collect_target_ids(value: Any, targets: set[str]) -> None:
    """Collect verifier target ids from nested task metadata."""

    if isinstance(value, list):
        for item in value:
            _collect_target_ids(item, targets)
        return
    if not isinstance(value, dict):
        return
    for key in ("item", "item_id", "block", "block_id", "entity", "entity_id", "name"):
        item = value.get(key)
        if isinstance(item, str) and item:
            targets.add(item)
    for key in ("all", "any"):
        _collect_target_ids(value.get(key), targets)


def _primary_target(
    run: RunRecord,
    action_plan: list[HarnessAction],
    target_ids: set[str],
) -> str:
    """Choose one stable target id for naming and summaries."""

    verifier_target = _verifier_primary_target(run)
    if verifier_target:
        return verifier_target
    if _programmatic_task_category(run) == "survival":
        verifier_type = _verifier_type(run)
        if verifier_type:
            return verifier_type
    if target_ids:
        return sorted(target_ids)[0]
    for action in action_plan:
        for key in ("item", "block", "entity"):
            value = action.args.get(key)
            if isinstance(value, str) and value:
                return value
    return _slug(run.task_id)


def derive_skill_name(run: RunRecord, primary_target: str) -> str:
    """Name programmatic skills from task category and verifier target.

    Action types describe how a task was solved, but they must not change the
    task family. For example, incidental self-defense during a harvest run must
    never turn ``harvest_white_wool`` into ``defeat_white_wool``.
    """

    category = _programmatic_task_category(run)
    prefix = PROGRAMMATIC_SKILL_NAME_PREFIXES.get(category)
    if prefix is None:
        return _slug(f"skill_{primary_target}")
    target = _skill_name_target(category, prefix, primary_target)
    return _slug(f"{prefix}_{target}")


def _programmatic_task_category(run: RunRecord) -> str:
    """Read one of the four programmatic categories, including legacy runs."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    for value in (task_spec.get("category"), task_spec.get("family")):
        category = _slug(str(value or "")).replace("_", "")
        if category in PROGRAMMATIC_SKILL_NAME_PREFIXES:
            return category
    task_id_tokens = set(_slug(run.task_id).split("_"))
    for category in PROGRAMMATIC_SKILL_NAME_PREFIXES:
        if category in task_id_tokens:
            return category
    return ""


def _skill_name_target(category: str, prefix: str, primary_target: str) -> str:
    """Remove redundant namespace/category prefixes from the canonical target."""

    target = _slug(primary_target)
    removable_prefixes = ("minedojo_", f"{category}_", f"{prefix}_")
    changed = True
    while changed:
        changed = False
        for removable in removable_prefixes:
            if target.startswith(removable) and len(target) > len(removable):
                target = target[len(removable) :]
                changed = True
    return target or "unknown"


def _verifier_type(run: RunRecord) -> str | None:
    """Return a stable verifier type when a task has no item/entity target."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    for metadata_field in ("verifier", "success_criteria"):
        value = task_spec.get(metadata_field)
        if not isinstance(value, dict):
            continue
        verifier_type = value.get("type")
        if isinstance(verifier_type, str) and verifier_type:
            return _slug(verifier_type)
    return None


def _verifier_primary_target(run: RunRecord) -> str | None:
    """Prefer explicit verifier targets over broad goal/tag tokens for skill identity."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    for metadata_field in ("verifier", "success_criteria"):
        target = _first_structured_target(task_spec.get(metadata_field))
        if target:
            return target
    return None


def _verifier_entity_target(run: RunRecord) -> str | None:
    """Return an explicit entity verifier target without conflating item prerequisites."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    for metadata_field in ("verifier", "success_criteria"):
        target = _first_entity_target(task_spec.get(metadata_field))
        if target:
            return target
    return None


def _first_entity_target(value: Any) -> str | None:
    """Find the first canonical entity field inside a nested verifier."""

    if isinstance(value, list):
        for item in value:
            target = _first_entity_target(item)
            if target:
                return target
        return None
    if not isinstance(value, dict):
        return None
    for key in ("entity", "entity_id"):
        target = value.get(key)
        if isinstance(target, str) and target:
            return target
    for key in ("all", "any"):
        target = _first_entity_target(value.get(key))
        if target:
            return target
    return None


def _step_entity_target(step: StepRecord) -> str | None:
    """Read an entity name from action arguments or normalized action results."""

    action = step.action if isinstance(step.action, dict) else {}
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    result = step.action_result if isinstance(step.action_result, dict) else {}
    candidates = [
        args.get("entity"),
        args.get("entity_id"),
        args.get("name"),
        result.get("entity"),
        result.get("query") if action.get("type") == "scan_entities" else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and not candidate.isdigit():
            return candidate
    return None


def _same_entity_target(left: str, right: str) -> bool:
    """Compare namespaced and un-namespaced entity ids deterministically."""

    return _canonical_entity_target(left) == _canonical_entity_target(right)


def _canonical_entity_target(value: str) -> str:
    """Normalize one entity id for evidence filtering."""

    return value.removeprefix("minecraft:").strip().lower()


def _first_structured_target(value: Any) -> str | None:
    """Return the first item/block/entity target from nested verifier metadata."""

    if isinstance(value, list):
        for item in value:
            target = _first_structured_target(item)
            if target:
                return target
        return None
    if not isinstance(value, dict):
        return None
    for key in ("item", "item_id", "block", "block_id", "entity", "entity_id", "name"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    for key in ("all", "any"):
        target = _first_structured_target(value.get(key))
        if target:
            return target
    return None


def _description(
    run: RunRecord,
    primary_target: str,
    action_plan: list[HarnessAction],
    policy_reason: str,
) -> str:
    """Render a reviewable natural-language skill summary."""

    goal = run.task_spec.get("goal") if isinstance(run.task_spec, dict) else None
    action_text = " -> ".join(action.type for action in action_plan)
    return (
        f"Reusable Minecraft procedure for {primary_target}. "
        f"Generalized from task {run.task_id}: {goal or 'unknown goal'}. "
        f"Policy reason: {policy_reason}. Source action pattern retained only for audit/replay: {action_text}."
    )


def _strategy_summary(
    run: RunRecord,
    primary_target: str,
    action_plan: list[HarnessAction],
    policy_reason: str,
) -> str:
    """Render a contextual strategy summary that avoids source-coordinate replay."""

    goal = run.task_spec.get("goal") if isinstance(run.task_spec, dict) else None
    actions = {action.type for action in action_plan}
    parts = [
        f"Use this as contextual guidance when the task requires {primary_target}; do not replay source coordinates blindly.",
        f"Original task: {goal or run.task_id}.",
        f"Creation signal: {policy_reason}.",
    ]
    if {"scan_blocks", "move_to", "dig_block_at"} & actions:
        parts.append(
            "First identify a current-world target from observation or scan evidence, then navigate near a reachable coordinate and act on that evidence-selected target."
        )
    if {"scan_dropped_items", "wait_ticks"} & actions:
        parts.append(
            "After changing the world, scan for dropped items, move into pickup range, wait briefly, and verify inventory/state."
        )
    if (
        "craft_item" in actions
        or _has_process_station(action_plan, "inventory")
        or _has_process_station(action_plan, "crafting_table")
    ):
        parts.append(
            "For crafting subtasks, verify ingredients and station first; recipes should come from knowledge tools when uncertain."
        )
    if "smelt_item" in actions or _has_process_station(action_plan, "furnace"):
        parts.append(
            "For furnace subtasks, place or find a furnace, verify input and fuel in inventory, then smelt the target output and verify inventory."
        )
    if "fight_entity" in actions:
        parts.append(
            "For combat subtasks, confirm the target entity is nearby or spawned by reset before attacking, and verify kill-stat progress."
        )
    if "scan_entities" in actions:
        parts.append(
            "For entity tasks, scan the target first to inspect distance, line of sight, airborne status, and whether melee or ranged mode is more promising."
        )
    if "follow" in actions:
        parts.append(
            "For a moving interaction target, keep following its selected entity_id during the next model turn, then interact with that same entity_id as the immediately following action."
        )
    if ENGAGEMENT_ACTIONS & actions:
        parts.append(
            "Use bounded combat engagements instead of one huge fight loop: choose melee or ranged mode from evidence, stop on low health or unreachable targets, then replan."
        )
    if "consume_item" in actions:
        parts.append(
            "When health or food becomes low, use available consumables before re-engaging or continuing long-running objectives."
        )
    return " ".join(parts)


def _triggers(
    run: RunRecord,
    action_plan: list[HarnessAction],
    target_ids: set[str],
    primary_target: str,
) -> list[str]:
    """Build search triggers from task family, target ids, and action semantics."""

    triggers: set[str] = {primary_target, *target_ids, _slug(run.task_id), run.task_id}
    if isinstance(run.task_spec, dict):
        for key in ("goal", "category", "family"):
            value = run.task_spec.get(key)
            if isinstance(value, str):
                triggers.update(_tokens(value))
        for tag in run.task_spec.get("knowledge_tags", []):
            if isinstance(tag, str):
                triggers.add(tag)
                triggers.update(_tokens(tag))
    for action in action_plan:
        triggers.add(str(action.type))
    return sorted(trigger for trigger in triggers if trigger)


def _preconditions(
    successful_steps: list[StepRecord],
    action_plan: list[HarnessAction],
    primary_target: str,
) -> list[str]:
    """Summarize reusable preconditions without overfitting to source coordinates."""

    preconditions: set[str] = set()
    action_types = {action.type for action in action_plan}
    if "scan_blocks" in action_types or "dig_block_at" in action_types:
        preconditions.add(f"target_block_visible_or_scannable:{primary_target}")
        preconditions.add("selected_target_must_be_reachable_or_near_reachable")
    if "scan_dropped_items" in action_types or "wait_ticks" in action_types:
        preconditions.add(f"drop_pickup_range_needed:{primary_target}")
    if "craft_item" in action_types or "process_item" in action_types:
        preconditions.add(f"recipe_inputs_available:{primary_target}")
    if "smelt_item" in action_types or _has_process_station(action_plan, "furnace"):
        preconditions.add(f"furnace_input_and_fuel_available:{primary_target}")
        preconditions.add("nearby_furnace_placed_or_available")
    if (
        {"scan_entities", "follow"} & set(action_types)
        or ENGAGEMENT_ACTIONS & set(action_types)
        or "fight_entity" in action_types
    ):
        preconditions.add(f"target_entity_visible_or_spawned:{primary_target}")
    if ENGAGEMENT_ACTIONS & set(action_types) or "fight_entity" in action_types:
        preconditions.add("combat_mode_should_match_reachability")
    if "consume_item" in action_types:
        preconditions.add("consumable_available_when_recovery_needed")
    first_observation = successful_steps[0].observation if successful_steps else {}
    for item in first_observation.get("inventory", []):
        if isinstance(item, dict) and item.get("name") and item.get("name") != primary_target:
            preconditions.add(f"inventory:{item['name']}")
    return sorted(preconditions)


def _task_scope(
    run: RunRecord, action_plan: list[HarnessAction], target_ids: set[str]
) -> list[str]:
    """Build scope tags for skill retrieval and deduplication."""

    scope: set[str] = {run.task_id, *target_ids}
    if isinstance(run.task_spec, dict):
        for key in ("category", "family"):
            value = run.task_spec.get(key)
            if isinstance(value, str):
                scope.add(value)
        for tag in run.task_spec.get("knowledge_tags", []):
            if isinstance(tag, str):
                scope.add(tag)
    for action in action_plan:
        scope.add(f"action:{action.type}")
    return sorted(scope)


def _dependencies(action_plan: list[HarnessAction], target_ids: set[str]) -> list[str]:
    """Extract action and target dependencies used by search and duplicate scoring."""

    dependencies: set[str] = set(target_ids)
    for action in action_plan:
        dependencies.add(f"action:{action.type}")
        for key in (
            "item",
            "item_id",
            "output",
            "input",
            "fuel",
            "block",
            "block_id",
            "entity",
            "entity_id",
            "station",
        ):
            value = action.args.get(key)
            if isinstance(value, str) and value:
                dependencies.add(value)
    return sorted(dependencies)


def _parameterized_plan(
    action_plan: list[HarnessAction], primary_target: str
) -> list[dict[str, Any]]:
    """Create a human-review generalized plan while preserving source action_plan separately."""

    plan: list[dict[str, Any]] = []
    for action in action_plan:
        if action.type == "scan_blocks":
            plan.append(
                {
                    "type": "scan_blocks",
                    "target": action.args.get("block") or primary_target,
                    "selection": "nearest_diggable_or_reachable_candidate",
                }
            )
        elif action.type == "move_to":
            plan.append(
                {
                    "type": "move_to",
                    "target": "selected_block_or_drop_position",
                    "recovery": "if no_path, use nearest_reachable_position or scan terrain before retrying",
                }
            )
        elif action.type == "follow":
            plan.append(
                {
                    "type": "follow",
                    "target": action.args.get("entity") or "selected_entity_id",
                    "selection": "prefer entity_id from scan_entities",
                    "follow_distance": action.args.get("follow_distance") or 1.25,
                    "stop_policy": "automatically stop immediately before the next action",
                }
            )
        elif action.type == "dig_block_at":
            plan.append(
                {
                    "type": "dig_block_at",
                    "target": action.args.get("block") or primary_target,
                    "position": "selected_block_position",
                    "postcondition": "block removed and drop may need scan_dropped_items/wait_ticks",
                }
            )
        elif action.type == "scan_dropped_items":
            plan.append(
                {
                    "type": "scan_dropped_items",
                    "target": action.args.get("item") or primary_target,
                    "selection": "nearest_reachable_drop_or_pickup_range_coordinate",
                }
            )
        elif action.type == "wait_ticks":
            plan.append(
                {"type": "wait_ticks", "purpose": "allow pickup or world state update to settle"}
            )
        elif action.type == "scan_entities":
            plan.append(
                {
                    "type": "scan_entities",
                    "target": action.args.get("entity") or primary_target,
                    "selection": "nearest_target_with_line_of_sight_and_mode_affordance",
                }
            )
        elif action.type == "equip_item":
            plan.append(
                {
                    "type": "equip_item",
                    "item": action.args.get("item")
                    or action.args.get("item_id")
                    or "inventory_selected_equipment",
                    "slot": action.args.get("slot") or "hand",
                    "purpose": "prepare current equipment before an interaction or bounded combat engagement",
                }
            )
        elif action.type in ENGAGEMENT_ACTIONS:
            plan.append(
                {
                    "type": action.type,
                    "target": action.args.get("entity") or primary_target,
                    "mode": action.args.get("mode") or "evidence_selected_mode",
                    "stop_policy": "return control on target_killed, low_health, target_unreachable, no_line_of_sight, no_ammo, or timeout",
                    "postcondition": "verify kill-stat delta or replan from tactical status",
                }
            )
        elif action.type == "consume_item":
            plan.append(
                {
                    "type": "consume_item",
                    "item": action.args.get("item")
                    or action.args.get("item_id")
                    or "available_consumable",
                    "purpose": "recover health or food before continuing combat or exploration",
                }
            )
        elif action.type == "smelt_item":
            plan.append(
                {
                    "type": "smelt_item",
                    "target": action.args.get("item")
                    or action.args.get("item_id")
                    or primary_target,
                    "input": action.args.get("input")
                    or action.args.get("input_item")
                    or "recipe_selected_input",
                    "fuel": action.args.get("fuel")
                    or action.args.get("fuel_item")
                    or "available_fuel",
                    "precondition": "nearby furnace exists and input/fuel are in inventory",
                    "postcondition": "verify output inventory delta",
                }
            )
        elif action.type == "process_item":
            station = _process_station(action)
            plan.append(
                {
                    "type": "process_item",
                    "station": station,
                    "target": action.args.get("output")
                    or action.args.get("item")
                    or action.args.get("item_id")
                    or primary_target,
                    "input": action.args.get("input")
                    or action.args.get("input_item")
                    or ("recipe_selected_input" if station == "furnace" else None),
                    "fuel": action.args.get("fuel")
                    or action.args.get("fuel_item")
                    or ("available_fuel" if station == "furnace" else None),
                    "precondition": "required station and inputs are available",
                    "postcondition": "verify output inventory delta",
                }
            )
        else:
            plan.append({"type": action.type, "args_template": _arg_template(action.args)})
    return plan


def _recovery_policy(action_plan: list[HarnessAction], primary_target: str) -> list[str]:
    """Build reusable recovery notes from the source action types."""

    action_types = {action.type for action in action_plan}
    notes: list[str] = []
    if "move_to" in action_types:
        notes.append(
            "If move_to reports no_path, use nearest_reachable_position or scan terrain before selecting a new coordinate."
        )
    if "follow" in action_types:
        notes.append(
            "For moving interaction targets, follow the selected entity_id at close range and make the intended interaction the next action so follow stops only at execution time."
        )
    if "dig_block_at" in action_types:
        notes.append(
            f"If the selected {primary_target} block is not diggable, move closer, pick another scanned candidate, or alter terrain explicitly."
        )
    if "scan_dropped_items" in action_types or "wait_ticks" in action_types:
        notes.append(
            "If inventory does not update after digging, scan dropped items and move to a reachable pickup-range coordinate before waiting again."
        )
    if "fight_entity" in action_types:
        notes.append(
            "If the target entity is not visible, scan/observe after reset before attacking and avoid assuming absence means success."
        )
    if "scan_entities" in action_types or ENGAGEMENT_ACTIONS & set(action_types):
        notes.append(
            "If moving-target melee combat reports target_unreachable or the entity is airborne, inspect inventory, equip suitable ranged gear with equip_item, and retry move_to_and_engage_combat in ranged mode."
        )
        notes.append(
            "If ranged combat reports no_line_of_sight, reposition with move_to or scan_entities again before re-engaging."
        )
    if "equip_item" in action_types or ENGAGEMENT_ACTIONS & set(action_types):
        notes.append(
            "Before move_to_and_engage_combat, verify the current equipment state; use equip_item when the desired weapon or shield is only present in inventory."
        )
    if "consume_item" in action_types or ENGAGEMENT_ACTIONS & set(action_types):
        notes.append(
            "If move_to_and_engage_combat returns low_health, consume a suitable item when available, then re-scan before continuing."
        )
    if "smelt_item" in action_types or _has_process_station(action_plan, "furnace"):
        notes.append(
            "If process_item station=furnace reports missing_station, place a furnace nearby; if it reports missing_input or missing_fuel, gather the input or fuel before retrying."
        )
    return notes


def _has_process_station(action_plan: list[HarnessAction], station: str) -> bool:
    """Return whether a process_item step uses a compatible station."""

    return any(
        action.type == "process_item" and _process_station(action) == station
        for action in action_plan
    )


def _process_station(action: HarnessAction) -> str:
    """Normalize process_item station metadata for skill summaries."""

    station = str(action.args.get("station") or "inventory").lower()
    if station in {"furnace", "smelt", "smelting"}:
        return "furnace"
    if station in {"crafting_table", "nearby_crafting_table", "workbench", "3x3"}:
        return "crafting_table"
    return "inventory"


def _source_evidence(
    run: RunRecord,
    successful_steps: list[StepRecord],
    action_plan: list[HarnessAction],
    primary_target: str,
    *,
    excluded_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture auditable source evidence without making it the execution policy."""

    return {
        "source_run_id": run.id,
        "source_task_id": run.task_id,
        "primary_target": primary_target,
        "source_step_indexes": [int(step.step_index) for step in successful_steps],
        "source_action_types": [action.type for action in action_plan],
        "excluded_source_steps": excluded_steps,
        "source_coordinates_are_replay_only": True,
    }


def _verifier_stats(run: RunRecord) -> dict[str, Any]:
    """Extract verifier metadata from the source task for skill review."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    return {
        "source_task_verifier": task_spec.get("verifier") or task_spec.get("success_criteria"),
        "source_run_status": run.status,
    }


def _arg_template(args: dict[str, Any]) -> dict[str, Any]:
    """Keep non-coordinate action arguments while marking coordinates as evidence-derived."""

    template: dict[str, Any] = {}
    for key, value in args.items():
        if key == "position" and isinstance(value, dict):
            template[key] = "evidence_selected_position"
        else:
            template[key] = value
    return template


def _tokens(text: str) -> set[str]:
    """Tokenize identifiers and prose for deterministic lexical matching."""

    return {token for token in re.split(r"[^a-zA-Z0-9_]+", text.lower()) if token}


def _slug(text: str) -> str:
    """Normalize text into a stable lower snake-case identifier."""

    return (
        "_".join(sorted(_tokens(text)))
        if " " in text
        else re.sub(r"[^a-zA-Z0-9_]+", "_", text.lower()).strip("_")
    )
