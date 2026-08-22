from __future__ import annotations

import copy
from typing import Any, Callable


ActionCompressor = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any], list[str]],
    dict[str, Any],
]

_SCAFFOLD_MATERIAL_HINT = (
    "If the route has height gaps, cliffs, or missing support, use existing inventory evidence or "
    "query_inventory to check for safe scaffold blocks such as dirt, cobblestone, stone, deepslate, "
    "netherrack, sand, or gravel. If there are not enough expendable blocks, gather common blocks "
    "such as dirt or stone before retrying move_to; use logs/planks only when they are not task-critical."
)

_ENTITY_SCAN_RELOCATION_HINT = (
    "A broad entity scan is a snapshot of the currently loaded area. If no returned entity "
    "satisfies the task criteria, including when memory rules out every candidate, move to a "
    "different reachable area tens of blocks away before another broad scan. Do not only increase "
    "count or max_distance from the same position. Re-scan in place only after evidence of a world "
    "or target change, movement from active follow, or when refreshing one exact entity_id."
)

_COMMON_ENTITY_METADATA_KEYS = frozenset(
    {
        "shared_flags",
        "air_supply",
        "custom_name",
        "custom_name_visible",
        "silent",
        "no_gravity",
        "pose",
        "ticks_frozen",
        "living_entity_flags",
        "health",
        "effect_color",
        "effect_ambience",
        "arrow_count",
        "stinger_count",
        "sleeping_pos",
        "mob_flags",
    }
)

_USEFUL_COMMON_ENTITY_METADATA_KEYS = (
    "custom_name",
    "pose",
    "health",
    "silent",
    "no_gravity",
)


def build_state_context(
    task_spec: dict[str, Any],
    observation: dict[str, Any],
    previous_step: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the model-facing state summary and compact structured evidence."""

    target_items = _target_items(task_spec)
    target_blocks = _target_blocks(task_spec)
    target_entities = _target_entities(task_spec)
    current_state = _current_state(observation, target_items, target_blocks, target_entities)
    previous_evidence = _compress_previous_step(previous_step, observation, target_items)
    goal_progress = _goal_progress(task_spec, observation)
    task_objective = _task_objective(task_spec)
    task_progress = _task_progress(task_objective, goal_progress)
    summary_parts = [
        _task_goal_summary(task_objective.get("goal")),
        _goal_progress_summary(goal_progress),
        _current_state_summary(current_state),
    ]
    progress_summary = _creative_progress_summary(current_state.get("creative_progress"))
    if progress_summary:
        summary_parts.append(progress_summary)
    if previous_evidence is not None:
        summary_parts.append(str(previous_evidence["summary"]))
    return {
        "state_summary": " ".join(part for part in summary_parts if part),
        "task_objective": task_objective,
        "task_progress": task_progress,
        "compact_evidence": {
            "goal_progress": goal_progress,
            "current_state": current_state,
            "previous_step": previous_evidence,
            "raw_evidence_available": True,
        },
    }


def compress_action_evidence(
    *,
    step_index: int,
    action: dict[str, Any],
    action_result: dict[str, Any],
    observation: dict[str, Any],
    task_spec: dict[str, Any],
) -> dict[str, Any]:
    """Build one action-specific compact trace entry for multi-step context memory."""

    return _compress_previous_step(
        {
            "step_index": step_index,
            "action": action,
            "action_result": action_result,
        },
        observation,
        _target_items(task_spec),
    ) or {
        "step_index": step_index,
        "action_type": str(action.get("type") or "unknown"),
        "ok": action_result.get("ok"),
        "summary": "No compact action evidence was available.",
        "blockers": [],
    }


def _compress_previous_step(
    previous_step: dict[str, Any] | None,
    observation: dict[str, Any],
    target_items: list[str],
) -> dict[str, Any] | None:
    """Dispatch one previous action/result pair to an action-specific compressor."""

    if not isinstance(previous_step, dict):
        return None
    action = previous_step.get("action")
    result = previous_step.get("action_result")
    if not isinstance(action, dict) or not isinstance(result, dict):
        return None
    action_type = str(action.get("type") or result.get("action_type") or "unknown")
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    compressor = _ACTION_COMPRESSORS.get(action_type, _compress_generic_action)
    evidence = compressor(args, result, observation, target_items)
    evidence["step_index"] = previous_step.get("step_index")
    evidence["action_type"] = action_type
    evidence["ok"] = result.get("ok")
    evidence["recoverable"] = result.get("recoverable")
    evidence["memory_sources"] = _memory_source_refs(
        previous_step.get("step_index"),
        action_type,
        result,
    )
    stopped_follow = _dict(result.get("persistent_follow_stopped"))
    if stopped_follow:
        evidence["persistent_follow_stopped"] = stopped_follow
        evidence["summary"] = (
            f"{evidence['summary']} Background follow of "
            f"{_dict(stopped_follow.get('target')).get('name') or 'the selected entity'} "
            f"stopped immediately before this action."
        )
    if result.get("error_code") is not None:
        error_code = str(result["error_code"])
        blockers = evidence.setdefault("blockers", [])
        if error_code not in blockers:
            blockers.append(error_code)
    return evidence


def _compress_scan_blocks(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress scan_blocks around the target query and nearest block candidates."""

    blocks = _list_of_dicts(result.get("blocks"))
    nearest = [_block_candidate(block) for block in blocks[:5]]
    query = result.get("query") or args.get("block") or args.get("block_id") or args.get("name")
    found_count = len(blocks)
    if result.get("ok") is True and nearest:
        first = nearest[0]
        summary = (
            f"scan_blocks({query}) succeeded. Found {found_count} candidates; nearest is "
            f"{first.get('name')} at {_format_position(first.get('position'))}, "
            f"distance {first.get('distance')}, can_dig={first.get('can_dig')}."
        )
        if first.get("can_dig") is False:
            summary += f" The nearest block is not immediately diggable. {_SCAFFOLD_MATERIAL_HINT}"
    elif result.get("ok") is True:
        summary = f"scan_blocks({query}) succeeded but found no matching blocks."
    else:
        summary = _failure_summary("scan_blocks", result)
    navigation_preparation_hint = (
        _SCAFFOLD_MATERIAL_HINT if nearest and nearest[0].get("can_dig") is False else None
    )
    return {
        "summary": summary,
        "query": query,
        "max_distance": result.get("max_distance") or args.get("max_distance"),
        "found_count": found_count,
        "nearest_targets": nearest,
        "navigation_preparation_hint": navigation_preparation_hint,
        "blockers": [],
    }


def _compress_scan_entities(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress scan_entities around target reachability and combat mode evidence."""

    entities = _list_of_dicts(result.get("entities"))
    nearest = [_entity_candidate(entity) for entity in entities[:5]]
    query = result.get("query") or args.get("entity") or args.get("entity_id") or args.get("name")
    if result.get("ok") is True and nearest:
        first = nearest[0]
        summary = (
            f"scan_entities({query}) succeeded. Found {len(entities)} entities; nearest is "
            f"{first.get('name')} at {_format_position(first.get('position'))}, "
            f"distance {first.get('distance')}, melee_reachable={first.get('melee_reachable')}, "
            f"suggested_modes={first.get('suggested_modes')}."
        )
        if _entity_needs_navigation_preparation(first):
            summary += f" The nearest entity is not immediately reachable by melee from the current position. {_SCAFFOLD_MATERIAL_HINT}"
    elif result.get("ok") is True:
        summary = (
            f"scan_entities({query}) succeeded but found no matching entities. "
            f"{_ENTITY_SCAN_RELOCATION_HINT}"
        )
    else:
        summary = _failure_summary("scan_entities", result)
    navigation_preparation_hint = (
        _SCAFFOLD_MATERIAL_HINT
        if nearest and _entity_needs_navigation_preparation(nearest[0])
        else None
    )
    return {
        "summary": summary,
        "query": query,
        "max_distance": result.get("max_distance") or args.get("max_distance"),
        "found_count": len(entities),
        "nearest_entities": nearest,
        "navigation_preparation_hint": navigation_preparation_hint,
        "exploration_hint": _ENTITY_SCAN_RELOCATION_HINT,
        "blockers": [],
    }


def _compress_scan_dropped_items(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress scan_dropped_items around nearest visible item drops."""

    dropped_items = _list_of_dicts(result.get("dropped_items"))
    nearest = [_dropped_item_candidate(item) for item in dropped_items[:5]]
    query = result.get("query") or args.get("item") or args.get("item_id") or args.get("name")
    if result.get("ok") is True and nearest:
        first = nearest[0]
        summary = (
            f"scan_dropped_items({query}) succeeded. Found {len(dropped_items)} drops; nearest is "
            f"{first.get('item')} at {_format_position(first.get('position'))}, "
            f"distance {first.get('distance')}."
        )
    elif result.get("ok") is True:
        summary = f"scan_dropped_items({query}) succeeded but found no matching dropped items."
    else:
        summary = _failure_summary("scan_dropped_items", result)
    return {
        "summary": summary,
        "query": query,
        "max_distance": result.get("max_distance") or args.get("max_distance"),
        "found_count": len(dropped_items),
        "nearest_drops": nearest,
        "blockers": [],
    }


def _compress_move_to(
    args: dict[str, Any],
    result: dict[str, Any],
    observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress move_to around target, final distance, and timeout state."""

    target = result.get("target") or args.get("position")
    position_after = _position(result.get("observation")) or _position(observation)
    nearest_reachable = _position_dict(result.get("nearest_reachable_position"))
    distance = _round_number(result.get("distance"))
    if result.get("ok") is True:
        summary = f"move_to({_format_position(target)}) succeeded. Final distance is {distance}."
    elif result.get("state_summary"):
        summary = (
            f"move_to({_format_position(target)}) failed with {result.get('error_code') or 'error'}. "
            f"{result.get('state_summary')} Position after action is {_format_position(position_after)}."
        )
        if nearest_reachable:
            summary += f" Nearest reachable position: {_format_position(nearest_reachable)}."
    else:
        summary = (
            f"move_to({_format_position(target)}) failed with {result.get('error_code') or 'error'}. "
            f"Position after action is {_format_position(position_after)}."
        )
        if nearest_reachable:
            summary += f" Nearest reachable position: {_format_position(nearest_reachable)}."
    failure_reason = result.get("navigation_failure_reason")
    if failure_reason and str(failure_reason) not in summary:
        summary += f" Navigation failure reason: {failure_reason}"
    inventory_delta = _dict(result.get("inventory_delta"))
    scaffolding_consumed = _dict(result.get("scaffolding_consumed"))
    if inventory_delta:
        summary += f" Inventory delta from move_to: {_format_delta(inventory_delta)}."
    if scaffolding_consumed:
        summary += f" Pathfinder consumed scaffold blocks: {_format_counts(scaffolding_consumed)}."
    return {
        "summary": summary,
        "target": _position_dict(target),
        "tolerance": result.get("tolerance") or args.get("tolerance"),
        "timeout_ms": result.get("timeout_ms") or args.get("timeout_ms"),
        "planning_timeout_ms": result.get("planning_timeout_ms"),
        "distance": distance,
        "position_after": position_after,
        "diagnosis": result.get("diagnosis"),
        "navigation_failure_reason": result.get("navigation_failure_reason"),
        "movement_policy": _dict(result.get("movement_policy")),
        "scaffolding_item_names": _list_of_strings(result.get("scaffolding_item_names")),
        "available_scaffolding_count": result.get("available_scaffolding_count"),
        "inventory_delta": inventory_delta,
        "consumed_items": _dict(result.get("consumed_items")),
        "scaffolding_delta": _dict(result.get("scaffolding_delta")),
        "scaffolding_consumed": scaffolding_consumed,
        "suggested_affordances": _list_of_dicts(result.get("suggested_affordances")),
        "nearest_reachable_position": nearest_reachable,
        "target_height_delta": _round_number(result.get("target_height_delta")),
        "path_summary": _dict(result.get("path_summary")),
        "blockers": [],
    }


def _compress_follow(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress cross-turn following around target identity and stop semantics."""

    target = _dict(result.get("target"))
    active_follow = _dict(result.get("active_follow"))
    target_name = (
        target.get("name")
        or args.get("entity")
        or args.get("name")
        or f"entity_id={args.get('entity_id')}"
    )
    if result.get("ok") is True:
        summary = (
            f"follow({target_name}) started at distance "
            f"{_round_number(target.get('distance'))}; it remains active during model reasoning "
            "and stops immediately before the next action."
        )
    else:
        summary = _failure_summary("follow", result)
    return {
        "summary": summary,
        "status": result.get("status"),
        "target": _entity_candidate(target) if target else None,
        "follow_distance": result.get("follow_distance") or args.get("follow_distance"),
        "max_distance": result.get("max_distance") or args.get("max_distance"),
        "persistent": result.get("persistent"),
        "until": result.get("until"),
        "active_follow": active_follow or None,
        "replaced_follow": _dict(result.get("replaced_follow")) or None,
        "recommended_next_actions": _list_of_strings(
            result.get("recommended_next_actions")
        ),
        "suggested_next_actions": result.get("suggested_next_actions"),
        "blockers": [],
    }


def _compress_dig_block_at(
    args: dict[str, Any],
    result: dict[str, Any],
    observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress dig_block_at around the block transition and inventory delta."""

    position = result.get("position") or args.get("position")
    block_before = result.get("block_before") or result.get("block")
    block_after = result.get("block_after")
    inventory_delta = _dict(result.get("inventory_delta"))
    spawned_drops = _list_of_dicts(result.get("spawned_drops"))
    drop_status = result.get("drop_observation_status")
    if result.get("ok") is True:
        summary = (
            f"dig_block_at({_format_position(position)}, {block_before}) succeeded. "
            f"Block changed from {block_before} to {block_after}. "
            f"Inventory delta: {_format_delta(inventory_delta)}. "
            f"Drop observation: {drop_status}; observed drops: "
            f"{_format_dropped_items(spawned_drops)}."
        )
    else:
        summary = _failure_summary("dig_block_at", result)
    return {
        "summary": summary,
        "position": _position_dict(position),
        "expected_block": args.get("block") or args.get("block_id") or args.get("name"),
        "block_before": block_before,
        "block_after": block_after,
        "world_delta": {
            "position": _position_dict(position),
            "before": block_before,
            "after": block_after,
        },
        "inventory_delta": inventory_delta,
        "held_item": result.get("held_item"),
        "estimated_dig_time_ms": result.get("estimated_dig_time_ms"),
        "block_removed": result.get("block_removed"),
        "spawned_drops": spawned_drops,
        "drop_observation_status": drop_status,
        "drop_evidence_source": result.get("drop_evidence_source"),
        "position_after": _position(result.get("observation")) or _position(observation),
        "blockers": [],
    }


def _compress_wait_ticks(
    args: dict[str, Any],
    result: dict[str, Any],
    observation: dict[str, Any],
    target_items: list[str],
) -> dict[str, Any]:
    """Compress wait_ticks around elapsed ticks and inventory changes."""

    inventory_delta = _dict(result.get("inventory_delta"))
    latest_observation = _dict(result.get("observation")) or observation
    inventory_counts = _inventory_counts(latest_observation)
    relevant_counts = _relevant_inventory_counts(inventory_counts, target_items)
    if result.get("ok") is True:
        summary = (
            f"wait_ticks({result.get('waited_ticks') or args.get('ticks')}) succeeded. "
            f"Inventory delta: {_format_delta(inventory_delta)}. "
            f"Relevant inventory: {_format_counts(relevant_counts)}."
        )
    else:
        summary = _failure_summary("wait_ticks", result)
    return {
        "summary": summary,
        "waited_ticks": result.get("waited_ticks") or args.get("ticks"),
        "inventory_delta": inventory_delta,
        "inventory_counts": relevant_counts,
        "blockers": [],
    }


def _compress_query_inventory(
    _args: dict[str, Any],
    result: dict[str, Any],
    observation: dict[str, Any],
    target_items: list[str],
) -> dict[str, Any]:
    """Compress query_inventory around target item counts."""

    inventory_source = (
        {"inventory": result.get("inventory")}
        if isinstance(result.get("inventory"), list)
        else observation
    )
    inventory_counts = _inventory_counts(inventory_source)
    relevant_counts = _relevant_inventory_counts(inventory_counts, target_items)
    summary = (
        f"query_inventory succeeded. Relevant inventory: {_format_counts(relevant_counts)}."
        if result.get("ok") is True
        else _failure_summary("query_inventory", result)
    )
    return {
        "summary": summary,
        "inventory": _inventory_items(inventory_source, target_items),
        "inventory_counts": relevant_counts,
        "blockers": [],
    }


def _compress_resolve_terms(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress resolve_terms around canonical ids and unresolved query text."""

    terms = _list_of_dicts(result.get("terms"))
    canonical_ids = [str(term.get("canonical_id")) for term in terms if term.get("canonical_id")]
    query = result.get("query") or args.get("text") or args.get("query")
    summary = (
        f"resolve_terms({query}) returned canonical ids: {', '.join(canonical_ids) or 'none'}."
        if result.get("ok") is True
        else _failure_summary("resolve_terms", result)
    )
    return {
        "summary": summary,
        "query": query,
        "canonical_ids": canonical_ids,
        "terms": [
            {
                "canonical_id": term.get("canonical_id"),
                "kind": term.get("kind"),
                "name": term.get("name"),
                "description": term.get("description"),
            }
            for term in terms[:8]
        ],
        "blockers": [],
    }


def _compress_get_recipe(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress get_recipe around ingredients and crafting station."""

    item = result.get("item") or args.get("item") or args.get("item_id")
    recipe = _dict(result.get("recipe"))
    if result.get("ok") is True and recipe:
        ingredients = (
            recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []
        )
        ingredient_text = ", ".join(
            f"{ingredient.get('item_id')}x{ingredient.get('count')}"
            for ingredient in ingredients
            if isinstance(ingredient, dict)
        )
        summary = (
            f"get_recipe({item}) returned {ingredient_text or 'no listed ingredients'} -> "
            f"{recipe.get('output')}x{recipe.get('output_count')} at {recipe.get('station')}."
        )
    else:
        summary = _failure_summary("get_recipe", result)
    return {
        "summary": summary,
        "item": item,
        "recipe": recipe,
        "blockers": [],
    }


def _compress_retrieve_docs(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress retrieve_docs around source ids and short snippets."""

    docs = _list_of_dicts(result.get("docs"))
    query = result.get("query") or args.get("query") or args.get("text")
    doc_refs = [
        {
            "id": document.get("id"),
            "title": document.get("title"),
            "tags": document.get("tags"),
            "content": document.get("content"),
            "truncated": document.get("truncated"),
        }
        for document in docs[:3]
    ]
    summary = (
        f"retrieve_docs({query}) returned {len(docs)} snippet(s): "
        f"{', '.join(str(document.get('id')) for document in docs[:3]) or 'none'}."
        if result.get("ok") is True
        else _failure_summary("retrieve_docs", result)
    )
    return {
        "summary": summary,
        "query": query,
        "scope": result.get("scope") or args.get("scope"),
        "docs": doc_refs,
        "blockers": [],
    }


def _compress_craft_item(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress craft_item around target, station, recipe availability, and output."""

    item = result.get("item") or args.get("item") or args.get("item_id") or args.get("name")
    station = result.get("station") or args.get("station") or "inventory"
    if result.get("ok") is True:
        summary = (
            f"craft_item({item}) succeeded at {station}. Expected output count: "
            f"{result.get('expected_output_count')}."
        )
    else:
        summary = _failure_summary("craft_item", result)
    return {
        "summary": summary,
        "item": item,
        "requested_count": result.get("count") or args.get("count"),
        "craft_count": result.get("craft_count"),
        "produced_per_craft": result.get("produced_per_craft"),
        "expected_output_count": result.get("expected_output_count"),
        "station": station,
        "blockers": [],
    }


def _compress_smelt_item(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress smelt_item around furnace, input/fuel, output, and inventory delta."""

    item = (
        result.get("item")
        or args.get("item")
        or args.get("item_id")
        or args.get("output")
        or args.get("name")
    )
    input_item = result.get("input") or args.get("input") or args.get("input_item")
    fuel = result.get("fuel") or args.get("fuel") or args.get("fuel_item")
    if result.get("ok") is True:
        summary = (
            f"smelt_item({item}) succeeded using input={input_item}, fuel={fuel}, "
            f"output_count={result.get('output_count')}."
        )
    else:
        summary = _failure_summary("smelt_item", result)
    return {
        "summary": summary,
        "item": item,
        "input": input_item,
        "fuel": fuel,
        "requested_count": result.get("count") or args.get("count"),
        "output_count": result.get("output_count"),
        "furnace_position": _position_dict(result.get("furnace_position")),
        "inventory_delta": _dict(result.get("inventory_delta")),
        "suggested_next_actions": result.get("suggested_next_actions"),
        "blockers": [],
    }


def _compress_process_item(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress process_item around station-specific crafting or smelting evidence."""

    station = (
        result.get("station")
        or args.get("station")
        or ("furnace" if result.get("fuel") else "inventory")
    )
    item = (
        result.get("item")
        or args.get("output")
        or args.get("item")
        or args.get("item_id")
        or args.get("name")
    )
    input_item = result.get("input") or args.get("input") or args.get("input_item")
    fuel = result.get("fuel") or args.get("fuel") or args.get("fuel_item")
    if result.get("ok") is True:
        if station == "furnace":
            summary = (
                f"process_item(station=furnace, output={item}) succeeded using "
                f"input={input_item}, fuel={fuel}, output_count={result.get('output_count')}."
            )
        else:
            summary = (
                f"process_item(station={station}, output={item}) succeeded. "
                f"Expected output count: {result.get('expected_output_count')}."
            )
    else:
        summary = _failure_summary("process_item", result)
    return {
        "summary": summary,
        "station": station,
        "item": item,
        "input": input_item,
        "fuel": fuel,
        "requested_count": result.get("count") or args.get("count"),
        "craft_count": result.get("craft_count"),
        "produced_per_craft": result.get("produced_per_craft"),
        "expected_output_count": result.get("expected_output_count"),
        "output_count": result.get("output_count"),
        "furnace_position": _position_dict(result.get("furnace_position")),
        "inventory_delta": _dict(result.get("inventory_delta")),
        "suggested_next_actions": result.get("suggested_next_actions"),
        "blockers": [],
    }


def _compress_place_block(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress place_block around placement target and support block."""

    item = result.get("item") or args.get("item") or args.get("block") or args.get("name")
    target = result.get("target") or args.get("position")
    summary = (
        f"place_block({item}) succeeded at {_format_position(target)}."
        if result.get("ok") is True
        else _failure_summary("place_block", result)
    )
    if result.get("ok") is not True and result.get("error_code") == "no_support_block":
        recovery_hint = result.get("recovery_hint")
        if isinstance(recovery_hint, str) and recovery_hint:
            summary = f"{summary} {recovery_hint}"
    return {
        "summary": summary,
        "item": item,
        "target": _position_dict(target),
        "reference": _position_dict(result.get("reference")),
        "face": _position_dict(result.get("face")),
        "current_position": _position_dict(result.get("current_position")),
        "nearby_valid_placements": _placement_candidates(result.get("nearby_valid_placements")),
        "placement_policy": _dict(result.get("placement_policy")),
        "suggested_next_actions": result.get("suggested_next_actions"),
        "blockers": [],
    }


def _compress_equip_item(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress equip_item around the item, slot, and before/after equipment state."""

    item = result.get("item") or args.get("item") or args.get("item_id") or args.get("name")
    slot = result.get("slot") or args.get("slot") or "hand"
    if result.get("ok") is True:
        summary = f"equip_item({item}, slot={slot}) succeeded. Equipment now: {_format_equipment(_equipment(result.get('equipment_after')))}."
    else:
        summary = _failure_summary("equip_item", result)
    return {
        "summary": summary,
        "item": item,
        "slot": slot,
        "equipment_before": _equipment(result.get("equipment_before")),
        "equipment_after": _equipment(result.get("equipment_after") or result.get("equipment")),
        "suggested_next_actions": result.get("suggested_next_actions"),
        "blockers": [],
    }


def _compress_use_item(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress use_item around activation target and missing-target failures."""

    activated = result.get("activated")
    item = result.get("item") or args.get("item") or args.get("item_id") or args.get("name")
    entity_id = result.get("entity_id")
    if entity_id is None:
        entity_id = args.get("entity_id")
    target = result.get("block") or result.get("entity") or args.get("block") or args.get("entity")
    inventory_delta = _dict(result.get("inventory_delta"))
    spawned_drops = _list_of_dicts(result.get("spawned_drops"))
    metadata_delta = _dict(result.get("metadata_delta"))
    observed_effect = result.get("observed_effect")
    summary = (
        f"use_item activated {activated or 'target'}"
        f"{f' entity_id={entity_id}' if entity_id is not None else ''} "
        f"with item {item}. "
        f"Observed effect={observed_effect}; inventory delta "
        f"{_format_counts(inventory_delta) if inventory_delta else 'none'}; "
        f"new drops {_format_spawned_drops(spawned_drops)}; "
        f"metadata changes {', '.join(metadata_delta) if metadata_delta else 'none'}."
        if result.get("ok") is True
        else _failure_summary("use_item", result)
    )
    return {
        "summary": summary,
        "item": item,
        "held_item": result.get("held_item"),
        "activated": activated,
        "target": target,
        "entity_id": entity_id,
        "inventory_delta": inventory_delta,
        "spawned_drops": spawned_drops[:8],
        "metadata_delta": metadata_delta,
        "target_details_before": _entity_details_candidate(
            result.get("target_details_before")
        ),
        "target_details_after": _entity_details_candidate(
            result.get("target_details_after")
        ),
        "effect_observation_ms": result.get("effect_observation_ms"),
        "observed_effect": observed_effect,
        "effect_evidence_source": result.get("effect_evidence_source"),
        "blockers": [],
    }


def _compress_consume_item(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress consume_item around recovery-relevant health, food, and inventory deltas."""

    item = result.get("item") or args.get("item") or args.get("item_id") or args.get("name")
    if result.get("ok") is True:
        summary = (
            f"consume_item({item}) succeeded. Health {result.get('health_before')} -> "
            f"{result.get('health_after')}; food {result.get('food_before')} -> {result.get('food_after')}."
        )
    else:
        summary = _failure_summary("consume_item", result)
    return {
        "summary": summary,
        "item": item,
        "health_before": result.get("health_before"),
        "health_after": result.get("health_after"),
        "health_delta": _round_number(result.get("health_delta")),
        "food_before": result.get("food_before"),
        "food_after": result.get("food_after"),
        "food_delta": _round_number(result.get("food_delta")),
        "inventory_delta": _dict(result.get("inventory_delta")),
        "blockers": [],
    }


def _compress_fight_entity(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress fight_entity around attack count and defeated flag."""

    entity = result.get("entity") or args.get("entity") or args.get("entity_id") or args.get("name")
    attacks = result.get("attacks")
    defeated = result.get("defeated")
    if result.get("ok") is True:
        summary = f"fight_entity({entity}) succeeded. defeated={defeated}, attacks={attacks}."
    else:
        summary = _failure_summary("fight_entity", result)
    return {
        "summary": summary,
        "entity": entity,
        "weapon": args.get("weapon"),
        "attacks": attacks,
        "defeated": defeated,
        "last_position": _position_dict(result.get("last_position")),
        "blockers": [],
    }


def _compress_engage_combat(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress bounded moving-target combat around tactical status and evidence."""

    action_name = str(result.get("action_type") or "move_to_and_engage_combat")
    entity = result.get("entity") or args.get("entity") or args.get("entity_id") or args.get("name")
    mode = result.get("mode") or args.get("mode") or "melee"
    status = result.get("status") or result.get("error_code")
    if result.get("ok") is True:
        summary = (
            f"{action_name}({entity}, mode={mode}) finished with status={status}. "
            f"attacks={result.get('attacks')}, shots={result.get('shots')}, "
            f"confirmed_kill_delta={result.get('confirmed_kill_delta', result.get('kill_stat_delta'))}, "
            f"kill_count_source={result.get('kill_count_source')}."
        )
    elif result.get("state_summary"):
        summary = (
            f"{action_name}({entity}, mode={mode}) returned status={status}. "
            f"{result.get('state_summary')}"
        )
    else:
        summary = _failure_summary(action_name, result)
    return {
        "summary": summary,
        "entity": entity,
        "mode": mode,
        "status": status,
        "weapon": args.get("weapon"),
        "actual_weapon": result.get("weapon") or result.get("current_weapon"),
        "equipment": _equipment(result.get("equipment")),
        "ammo": args.get("ammo"),
        "attacks": result.get("attacks"),
        "shots": result.get("shots"),
        "kill_stat_delta": result.get("kill_stat_delta"),
        "confirmed_kill_delta": result.get("confirmed_kill_delta"),
        "kill_count_source": result.get("kill_count_source"),
        "kill_evidence": _list_of_dicts(result.get("kill_evidence"))[:4],
        "final_health": result.get("final_health"),
        "final_food": result.get("final_food"),
        "target": _entity_candidate(_dict(result.get("target")))
        if isinstance(result.get("target"), dict)
        else None,
        "reachability_scope": result.get("reachability_scope"),
        "tracking_duration_ms": result.get("tracking_duration_ms"),
        "unreachable_timeout_ms": result.get("unreachable_timeout_ms"),
        "stalled_for_ms": result.get("stalled_for_ms"),
        "initial_distance": _round_number(result.get("initial_distance")),
        "closest_distance": _round_number(result.get("closest_distance")),
        "final_distance": _round_number(result.get("final_distance")),
        "distance_progress": _round_number(result.get("distance_progress")),
        "initial_height_delta": _round_number(result.get("initial_height_delta")),
        "final_height_delta": _round_number(result.get("final_height_delta")),
        "target_airborne": result.get("target_airborne"),
        "melee_reachable": result.get("melee_reachable"),
        "follow_updates": result.get("follow_updates"),
        "diagnosis": result.get("diagnosis"),
        "recovery_guidance": _list_of_strings(result.get("recovery_guidance")),
        "suggested_modes": result.get("suggested_modes"),
        "suggested_next_actions": result.get("suggested_next_actions"),
        "combat_events": _list_of_dicts(result.get("combat_events"))[:6],
        "blockers": [],
    }


def _compress_request_visual_snapshot(
    _args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress request_visual_snapshot around availability and reference metadata."""

    snapshot = _dict(result.get("snapshot"))
    image = snapshot.get("image")
    summary = (
        "request_visual_snapshot captured a visual frame."
        if image
        else f"request_visual_snapshot unavailable: {snapshot.get('reason') or result.get('message') or 'no image'}."
    )
    return {
        "summary": summary,
        "snapshot_available": bool(image),
        "image": image,
        "format": snapshot.get("format"),
        "reason": snapshot.get("reason"),
        "blockers": [],
    }


def _compress_execute_skill(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress execute_skill around skill identity and failed substep evidence."""

    name = result.get("name") or args.get("name")
    version = result.get("version") or args.get("version")
    failed_step = result.get("failed_step") or result.get("failed_substep")
    summary = (
        f"execute_skill({name}@{version}) succeeded."
        if result.get("ok") is True
        else f"execute_skill({name}@{version}) failed at {failed_step or 'unknown step'}."
    )
    return {
        "summary": summary,
        "name": name,
        "version": version,
        "failed_step": failed_step,
        "substep_count": result.get("substep_count"),
        "inventory_delta": _dict(result.get("inventory_delta")),
        "blockers": [],
    }


def _compress_submit_for_evaluation(
    _args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress a finish request around evaluator acceptance and rejection evidence."""

    accepted = result.get("submission_accepted") is True
    summary = str(
        result.get("summary")
        or (
            "Finish request accepted for authoritative evaluation."
            if accepted
            else "Finish request rejected; continue acting from verifier evidence."
        )
    )
    verifier = _dict(result.get("verifier"))
    return {
        "summary": summary,
        "submission_accepted": accepted,
        "evaluation_status": result.get("evaluation_status"),
        "task_success": result.get("task_success"),
        "verifier_reason": verifier.get("reason"),
        "blockers": [] if accepted else [str(result.get("error_code") or "submission_rejected")],
    }


def _compress_generic_action(
    args: dict[str, Any],
    result: dict[str, Any],
    _observation: dict[str, Any],
    _target_items: list[str],
) -> dict[str, Any]:
    """Compress unknown actions without leaking large nested runtime payloads."""

    action_type = result.get("action_type") or "unknown"
    summary = (
        f"{action_type} succeeded."
        if result.get("ok") is True
        else _failure_summary(str(action_type), result)
    )
    return {
        "summary": summary,
        "args_keys": sorted(str(key) for key in args.keys()),
        "result_keys": sorted(str(key) for key in result.keys() if key != "observation"),
        "inventory_delta": _dict(result.get("inventory_delta")),
        "blockers": [],
    }


_ACTION_COMPRESSORS: dict[str, ActionCompressor] = {
    "resolve_terms": _compress_resolve_terms,
    "get_recipe": _compress_get_recipe,
    "retrieve_docs": _compress_retrieve_docs,
    "scan_blocks": _compress_scan_blocks,
    "scan_entities": _compress_scan_entities,
    "scan_dropped_items": _compress_scan_dropped_items,
    "move_to": _compress_move_to,
    "follow": _compress_follow,
    "dig_block_at": _compress_dig_block_at,
    "wait_ticks": _compress_wait_ticks,
    "query_inventory": _compress_query_inventory,
    "process_item": _compress_process_item,
    "craft_item": _compress_craft_item,
    "smelt_item": _compress_smelt_item,
    "place_block": _compress_place_block,
    "equip_item": _compress_equip_item,
    "use_item": _compress_use_item,
    "consume_item": _compress_consume_item,
    "move_to_and_engage_combat": _compress_engage_combat,
    "engage_combat": _compress_engage_combat,
    "fight_entity": _compress_fight_entity,
    "request_visual_snapshot": _compress_request_visual_snapshot,
    "execute_skill": _compress_execute_skill,
    "submit_for_evaluation": _compress_submit_for_evaluation,
}


def _current_state(
    observation: dict[str, Any],
    target_items: list[str],
    target_blocks: list[str],
    target_entities: list[str],
) -> dict[str, Any]:
    """Compress the current observation around position, inventory, and relevant nearby targets."""

    return {
        "position": _position(observation),
        "health": observation.get("health"),
        "food": observation.get("food"),
        "inventory": _inventory_items(observation, target_items),
        "equipment": _equipment(observation.get("equipment")),
        "active_follow": _dict(observation.get("active_follow")) or None,
        "nearby_blocks": _relevant_blocks(observation, target_blocks),
        "observed_dropped_items": _relevant_dropped_items(observation),
        "nearby_entities": _relevant_entities(observation, target_entities),
        "threat_pause": _threat_pause(observation),
        "creative_progress": _creative_progress(observation.get("creative_progress")),
    }


def _goal_progress(task_spec: dict[str, Any], observation: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarize verifier progress that can be inferred from the current observation."""

    inventory_counts = _inventory_counts(observation)
    initial_counts = _inventory_counts({"inventory": task_spec.get("_initial_inventory") or []})
    require_inventory_delta = _requires_inventory_delta(task_spec)
    progress: list[dict[str, Any]] = []
    for item in _target_items_with_counts(task_spec):
        if require_inventory_delta:
            initial_count = initial_counts.get(item["item"], 0)
            current_count = inventory_counts.get(item["item"], 0)
            current_delta = current_count - initial_count
            progress.append(
                {
                    "type": "inventory_delta_contains",
                    "item": item["item"],
                    "initial_count": initial_count,
                    "current_delta": current_delta,
                    "inventory_count": current_count,
                    "target_delta": item["count"],
                    "satisfied": current_delta >= item["count"],
                }
            )
            continue
        progress.append(
            {
                "type": "inventory_contains",
                "item": item["item"],
                "current": inventory_counts.get(item["item"], 0),
                "target": item["count"],
                "satisfied": inventory_counts.get(item["item"], 0) >= item["count"],
            }
        )
    for block in _target_blocks(task_spec):
        progress.append({"type": "block_target", "block": block})
    for entity in _target_entities(task_spec):
        progress.append({"type": "entity_target", "entity": entity})
    return progress


def _task_objective(task_spec: dict[str, Any]) -> dict[str, Any]:
    """Keep the exact task goal and completion criteria visible on every model turn."""

    minedojo = _dict(task_spec.get("minedojo"))
    goal = (
        task_spec.get("goal")
        or minedojo.get("official_prompt")
        or task_spec.get("description")
        or task_spec.get("task_id")
        or "unspecified task"
    )
    return {
        "task_id": task_spec.get("task_id"),
        "goal": str(goal),
        "verifier": copy.deepcopy(task_spec.get("verifier")),
        "success_criteria": copy.deepcopy(task_spec.get("success_criteria")),
        "completion_authority": "task_evaluator",
    }


def _task_progress(
    task_objective: dict[str, Any],
    goal_progress: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe current completion evidence without treating local actions as global failure."""

    observed_checks = [
        check.get("satisfied")
        for check in goal_progress
        if isinstance(check.get("satisfied"), bool)
    ]
    if observed_checks and all(observed_checks):
        completion_status = "goal_satisfied_by_current_observation"
    elif observed_checks and any(value is False for value in observed_checks):
        completion_status = "goal_not_yet_satisfied"
    else:
        completion_status = "not_inferable_from_current_observation"
    return {
        "task_id": task_objective.get("task_id"),
        "goal": task_objective.get("goal"),
        "completion_status": completion_status,
        "completion_authority": task_objective.get("completion_authority"),
        "progress_summary": _goal_progress_summary(goal_progress),
        "checks": copy.deepcopy(goal_progress),
    }


def _target_items(task_spec: dict[str, Any]) -> list[str]:
    """Extract target item ids from verifier-like task metadata."""

    return [item["item"] for item in _target_items_with_counts(task_spec)]


def _target_items_with_counts(task_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract target item ids and counts from nested verifier metadata."""

    targets: list[dict[str, Any]] = []
    _collect_targets(task_spec.get("verifier"), targets, [], [])
    _collect_targets(task_spec.get("success_criteria"), targets, [], [])
    unique: dict[str, int] = {}
    for target in targets:
        item = str(target["item"])
        unique[item] = max(unique.get(item, 0), int(target.get("count", 1)))
    return [{"item": item, "count": count} for item, count in sorted(unique.items())]


def _target_blocks(task_spec: dict[str, Any]) -> list[str]:
    """Extract target block ids from verifier-like task metadata."""

    blocks: list[str] = []
    _collect_targets(task_spec.get("verifier"), [], blocks, [])
    _collect_targets(task_spec.get("success_criteria"), [], blocks, [])
    return sorted(set(blocks))


def _target_entities(task_spec: dict[str, Any]) -> list[str]:
    """Extract target entity ids from verifier-like task metadata."""

    entities: list[str] = []
    _collect_targets(task_spec.get("verifier"), [], [], entities)
    _collect_targets(task_spec.get("success_criteria"), [], [], entities)
    return sorted(set(entities))


def _collect_targets(
    value: Any,
    items: list[dict[str, Any]],
    blocks: list[str],
    entities: list[str],
) -> None:
    """Collect verifier targets from nested dictionaries and lists."""

    if isinstance(value, list):
        for item in value:
            _collect_targets(item, items, blocks, entities)
        return
    if not isinstance(value, dict):
        return
    verifier_type = value.get("type")
    if verifier_type in {"inventory_contains", "inventory_delta_contains"}:
        item = value.get("item") or value.get("item_id") or value.get("name")
        if item:
            items.append({"item": str(item), "count": int(value.get("count", 1))})
    if verifier_type == "block_placed":
        block = value.get("block") or value.get("block_id") or value.get("name")
        if block:
            blocks.append(str(block))
    if verifier_type == "entity_defeated":
        entity = value.get("entity") or value.get("entity_id") or value.get("name")
        if entity:
            entities.append(str(entity))
    for key in ("all", "any"):
        _collect_targets(value.get(key), items, blocks, entities)


def _requires_inventory_delta(task_spec: dict[str, Any]) -> bool:
    """Return whether inventory targets must be newly obtained during this run."""

    if bool(task_spec.get("require_inventory_delta")):
        return True
    return _verifier_requires_inventory_delta(
        task_spec.get("verifier")
    ) or _verifier_requires_inventory_delta(task_spec.get("success_criteria"))


def _verifier_requires_inventory_delta(value: Any) -> bool:
    """Detect delta-style inventory verifier semantics in nested verifier specs."""

    if isinstance(value, list):
        return any(_verifier_requires_inventory_delta(item) for item in value)
    if not isinstance(value, dict):
        return False
    verifier_type = value.get("type")
    mode = value.get("mode") or value.get("comparison")
    if verifier_type == "inventory_delta_contains" or mode in {"delta", "inventory_delta"}:
        return True
    if value.get("require_delta") or value.get("delta"):
        return True
    return any(_verifier_requires_inventory_delta(value.get(key)) for key in ("all", "any"))


def _current_state_summary(current_state: dict[str, Any]) -> str:
    """Render a compact one-sentence summary of current position and inventory."""

    inventory = current_state.get("inventory")
    inventory_text = _format_inventory(inventory if isinstance(inventory, list) else [])
    equipment_text = _format_equipment(current_state.get("equipment"))
    dropped = current_state.get("observed_dropped_items")
    dropped_text = _format_dropped_items(dropped if isinstance(dropped, list) else [])
    threat_text = _format_threat_pause(current_state.get("threat_pause"))
    active_follow = _dict(current_state.get("active_follow"))
    follow_target = _dict(active_follow.get("target"))
    follow_text = (
        f" Active follow: {follow_target.get('name') or follow_target.get('id')} "
        f"at distance target {active_follow.get('follow_distance')}, until next action."
        if active_follow
        else ""
    )
    return (
        f"Current position is {_format_position(current_state.get('position'))}. "
        f"Inventory: {inventory_text}. Equipment: {equipment_text}. "
        f"Observed dropped item entities: {dropped_text}.{follow_text} {threat_text}"
    )


def _creative_progress(value: Any) -> dict[str, Any] | None:
    """Retain only bounded advisory MineCLIP fields from an online progress snapshot."""

    if not isinstance(value, dict):
        return None
    latest = value.get("latest") if isinstance(value.get("latest"), dict) else None
    compact_latest = None
    if latest is not None:
        compact_latest = {
            key: latest.get(key)
            for key in (
                "job_id",
                "status",
                "action_type",
                "score",
                "score_delta",
                "baseline_score",
                "trend",
                "confidence",
                "summary",
                "advisory_only",
                "success_authority",
            )
            if latest.get(key) is not None
        }
    return {
        "latest": compact_latest,
        "pending_jobs": value.get("pending_jobs"),
        "buffer_ready": value.get("buffer_ready"),
        "advisory_only": True,
        "success_authority": "human_review",
    }


def _creative_progress_summary(value: Any) -> str:
    """Render online MineCLIP feedback without presenting it as a correctness signal."""

    if not isinstance(value, dict):
        return ""
    latest = value.get("latest")
    if isinstance(latest, dict) and isinstance(latest.get("summary"), str):
        return str(latest["summary"])
    pending = value.get("pending_jobs")
    if isinstance(pending, int) and pending > 0:
        return f"MineCLIP advisory feedback has {pending} asynchronous job(s) pending."
    return ""


def _goal_progress_summary(goal_progress: list[dict[str, Any]]) -> str:
    """Render verifier progress as short natural-language text."""

    if not goal_progress:
        return ""
    parts: list[str] = []
    for item in goal_progress:
        if item.get("type") == "inventory_contains":
            parts.append(f"{item.get('item')} {item.get('current')}/{item.get('target')}")
        elif item.get("type") == "inventory_delta_contains":
            parts.append(
                f"new {item.get('item')} +{item.get('current_delta')}/+{item.get('target_delta')} "
                f"(inventory has {item.get('inventory_count')}; initial {item.get('initial_count')}; "
                "pre-task items do not count)"
            )
        elif item.get("type") == "block_target":
            parts.append(f"block target {item.get('block')}")
        elif item.get("type") == "entity_target":
            parts.append(f"entity target {item.get('entity')}")
    return f"Goal progress: {', '.join(parts)}."


def _task_goal_summary(value: Any) -> str:
    """Render the exact task goal prominently without duplicating punctuation."""

    if value is None:
        return ""
    goal = str(value).strip()
    if not goal:
        return ""
    suffix = "" if goal.endswith((".", "!", "?")) else "."
    return f"Task goal: {goal}{suffix}"


def _position(source: Any) -> dict[str, float] | None:
    """Extract a rounded position dict from an observation-like object."""

    if isinstance(source, dict) and isinstance(source.get("position"), dict):
        return _position_dict(source["position"])
    return None


def _position_dict(value: Any) -> dict[str, float] | None:
    """Convert one position-like mapping into rounded x/y/z floats."""

    if not isinstance(value, dict):
        return None
    if not {"x", "y", "z"} <= set(value):
        return None
    return {
        "x": _round_number(value.get("x")),
        "y": _round_number(value.get("y")),
        "z": _round_number(value.get("z")),
    }


def _block_candidate(block: dict[str, Any]) -> dict[str, Any]:
    """Compress one block candidate into model-facing target evidence."""

    return {
        "name": block.get("name") or block.get("block"),
        "position": _position_dict(block.get("position")),
        "distance": _round_number(block.get("distance")),
        "can_dig": block.get("can_dig"),
        "block_before": block.get("block_before"),
        "block_after": block.get("block_after"),
    }


def _entity_candidate(entity: dict[str, Any]) -> dict[str, Any]:
    """Compress one entity candidate into model-facing combat evidence."""

    entity_id = entity.get("entity_id")
    if entity_id is None:
        entity_id = entity.get("id")
    return {
        "entity_id": entity_id,
        "id": entity_id,
        "name": entity.get("name") or entity.get("entity"),
        "type": entity.get("type"),
        "display_name": entity.get("display_name"),
        "position": _position_dict(entity.get("position")),
        "distance": _round_number(entity.get("distance")),
        "height_delta": _round_number(entity.get("height_delta")),
        "line_of_sight": entity.get("line_of_sight"),
        "target_airborne": entity.get("target_airborne"),
        "melee_reachable": entity.get("melee_reachable"),
        "suggested_modes": entity.get("suggested_modes"),
        "details": _entity_details_candidate(entity.get("details")),
    }


def _entity_details_candidate(value: Any) -> dict[str, Any] | None:
    """Compress generic server entity details while retaining entity-specific metadata."""

    details = _dict(value)
    if not details:
        return None
    metadata = _dict(details.get("metadata"))
    entity_specific_keys = [
        key for key in metadata if key not in _COMMON_ENTITY_METADATA_KEYS
    ]
    retained_keys = entity_specific_keys[:12]
    for key in _USEFUL_COMMON_ENTITY_METADATA_KEYS:
        if key in metadata and key not in retained_keys and len(retained_keys) < 12:
            retained_keys.append(key)
    retained_metadata = {key: metadata.get(key) for key in retained_keys}
    decoded_metadata = _dict(details.get("metadata_decoded"))
    decoder = _dict(details.get("metadata_decoder"))
    return {
        "source": details.get("source"),
        "minecraft_version": details.get("minecraft_version"),
        "entity_type_id": details.get("entity_type_id"),
        "registry_name": details.get("registry_name"),
        "registry_display_name": details.get("registry_display_name"),
        "registry_type": details.get("registry_type"),
        "registry_category": details.get("registry_category"),
        "kind": details.get("kind"),
        "dimensions": _dict(details.get("dimensions")),
        "on_ground": details.get("on_ground"),
        "is_valid": details.get("is_valid"),
        "health": details.get("health"),
        "equipment": _list_of_dicts(details.get("equipment"))[:5],
        "effects": _list_of_dicts(details.get("effects"))[:8],
        "metadata_available": details.get("metadata_available"),
        "metadata": retained_metadata,
        "metadata_decoded": decoded_metadata,
        "metadata_decoder": {
            "available": decoder.get("available"),
            "minecraft_version": decoder.get("minecraft_version"),
            "decoder_revision": decoder.get("decoder_revision"),
            "semantic_source": _dict(decoder.get("semantic_source")),
            "recognized_fields": _list_of_strings(decoder.get("recognized_fields")),
        },
        "metadata_compression": {
            "raw_field_count": len(metadata),
            "retained_field_count": len(retained_metadata),
            "decoded_field_count": len(decoded_metadata),
            "policy": "entity_specific_then_useful_common_plus_semantic_decoding",
        },
    }


def _memory_source_refs(
    step_index: Any,
    action_type: str,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expose resolvable source handles without duplicating raw action results."""

    if not isinstance(step_index, int) or step_index < 0:
        return []
    refs: list[dict[str, Any]] = [
        {
            "source_ref": f"step:{step_index}/action_result",
            "scope": "entire_action_result",
        }
    ]
    for entity in _list_of_dicts(result.get("entities"))[:8]:
        entity_id = entity.get("entity_id")
        if entity_id is None:
            entity_id = entity.get("id")
        if entity_id is None:
            continue
        refs.append(
            {
                "source_ref": f"step:{step_index}/{action_type}/entity:{entity_id}",
                "scope": "selected_entity",
                "entity_id": entity_id,
            }
        )
    return refs


def _entity_needs_navigation_preparation(entity: dict[str, Any]) -> bool:
    """Return true when an entity scan suggests terrain preparation may help reach the target."""

    if entity.get("melee_reachable") is not False:
        return False
    if entity.get("target_airborne") is True:
        return False
    height_delta = entity.get("height_delta")
    if isinstance(height_delta, (int, float)) and abs(height_delta) >= 1.5:
        return True
    return entity.get("line_of_sight") is False


def _dropped_item_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Compress one visible dropped item into model-facing target evidence."""

    dropped = item.get("dropped_item") if isinstance(item.get("dropped_item"), dict) else {}
    return {
        "entity_id": item.get("entity_id") or item.get("id"),
        "item": item.get("item") or dropped.get("name"),
        "count": item.get("count") or dropped.get("count"),
        "position": _position_dict(item.get("position")),
        "distance": _round_number(item.get("distance")),
    }


def _placement_candidates(value: Any) -> list[dict[str, Any]]:
    """Compress valid placement candidates returned by the worker."""

    candidates: list[dict[str, Any]] = []
    for item in _list_of_dicts(value):
        candidates.append(
            {
                "target": _position_dict(item.get("target")),
                "reference": _position_dict(item.get("reference")),
                "reference_block": item.get("reference_block"),
                "face": _position_dict(item.get("face")),
            }
        )
    return candidates


def _relevant_blocks(observation: dict[str, Any], target_blocks: list[str]) -> list[dict[str, Any]]:
    """Return nearby blocks relevant to known target block ids."""

    blocks = _list_of_dicts(observation.get("nearby_blocks"))
    if target_blocks:
        blocks = [block for block in blocks if block.get("name") in target_blocks]
    return [_block_candidate(block) for block in blocks[:8]]


def _relevant_dropped_items(observation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return actually observed dropped item entities without verifier filtering."""

    entities = [
        entity
        for entity in _list_of_dicts(observation.get("nearby_entities"))
        if isinstance(entity.get("dropped_item"), dict)
    ]
    return [_dropped_item_candidate(entity) for entity in entities[:8]]


def _relevant_entities(
    observation: dict[str, Any], target_entities: list[str]
) -> list[dict[str, Any]]:
    """Return nearby entities relevant to known target entity ids."""

    entities = _list_of_dicts(observation.get("nearby_entities"))
    if target_entities:
        entities = [
            entity
            for entity in entities
            if entity.get("name") in target_entities or entity.get("type") in target_entities
        ]
    relevant = [_entity_candidate(entity) for entity in entities[:8]]
    for index, entity in enumerate(entities[:8]):
        if entity.get("dropped_item") is not None:
            relevant[index]["dropped_item"] = entity.get("dropped_item")
    return relevant


def _threat_pause(observation: dict[str, Any]) -> dict[str, Any] | None:
    """Return compact threat-pause metadata produced by the runtime observe phase."""

    pause = observation.get("threat_pause")
    if not isinstance(pause, dict):
        return None
    threats = _list_of_dicts(pause.get("threats"))
    return {
        "enabled": True,
        "should_pause": bool(pause.get("should_pause")),
        "already_paused": bool(pause.get("already_paused")),
        "world_frozen_for_model_decision": bool(pause.get("should_pause")),
        "threats": [_entity_candidate(threat) for threat in threats[:5]],
    }


def _inventory_items(observation: dict[str, Any], target_items: list[str]) -> list[dict[str, Any]]:
    """Return compact inventory rows, prioritizing verifier-relevant items."""

    inventory = _list_of_dicts(observation.get("inventory"))
    if target_items:
        relevant = [item for item in inventory if item.get("name") in target_items]
        if relevant:
            return [
                {"name": item.get("name"), "count": item.get("count")} for item in relevant[:12]
            ]
    return [{"name": item.get("name"), "count": item.get("count")} for item in inventory[:12]]


def _inventory_counts(observation: dict[str, Any]) -> dict[str, int]:
    """Return inventory counts keyed by item name."""

    counts: dict[str, int] = {}
    for item in _list_of_dicts(observation.get("inventory")):
        name = item.get("name")
        if name:
            counts[str(name)] = counts.get(str(name), 0) + int(item.get("count", 0))
    return counts


def _equipment(value: Any) -> dict[str, dict[str, Any] | None]:
    """Normalize an equipment snapshot to stable prompt-facing slot names."""

    if not isinstance(value, dict):
        return {}
    slots = ("main_hand", "off_hand", "head", "chest", "legs", "feet")
    normalized: dict[str, dict[str, Any] | None] = {}
    for slot in slots:
        item = value.get(slot)
        if isinstance(item, dict):
            normalized[slot] = {"name": item.get("name"), "count": item.get("count")}
        else:
            normalized[slot] = None
    return normalized


def _relevant_inventory_counts(counts: dict[str, int], target_items: list[str]) -> dict[str, int]:
    """Return target counts when present, otherwise a capped count dictionary."""

    if target_items:
        return {item: counts.get(item, 0) for item in target_items}
    return dict(list(counts.items())[:12])


def _failure_summary(action_name: str, result: dict[str, Any]) -> str:
    """Render a normalized failure summary for an action result."""

    error = result.get("error_code") or "error"
    message = result.get("message") or "No message."
    return f"{action_name} failed with {error}: {message}"


def _format_position(position: Any) -> str:
    """Format a position-like dict as short text."""

    parsed = (
        _position_dict(position)
        if not (isinstance(position, dict) and {"x", "y", "z"} <= set(position))
        else position
    )
    if not isinstance(parsed, dict):
        return "unknown"
    return f"({parsed.get('x')},{parsed.get('y')},{parsed.get('z')})"


def _format_delta(delta: dict[str, Any]) -> str:
    """Format inventory deltas in compact +N item form."""

    if not delta:
        return "none"
    parts = []
    for item, count in delta.items():
        try:
            value = int(count)
            parts.append(f"{value:+d} {item}")
        except (TypeError, ValueError):
            parts.append(f"{count} {item}")
    return ", ".join(parts)


def _format_inventory(inventory: list[dict[str, Any]]) -> str:
    """Format compact inventory rows."""

    if not inventory:
        return "empty"
    return ", ".join(f"{item.get('name')} x{item.get('count')}" for item in inventory)


def _format_equipment(equipment: Any) -> str:
    """Format equipped items in compact slot=name form."""

    parsed = _equipment(equipment)
    if not parsed:
        return "unknown"
    parts: list[str] = []
    for slot in ("main_hand", "off_hand", "head", "chest", "legs", "feet"):
        item = parsed.get(slot)
        if isinstance(item, dict) and item.get("name"):
            parts.append(f"{slot}={item.get('name')}")
    return ", ".join(parts) if parts else "empty"


def _format_dropped_items(items: list[dict[str, Any]]) -> str:
    """Format visible dropped item rows for compact prompt summaries."""

    if not items:
        return "none"
    return ", ".join(
        f"{item.get('item')} x{item.get('count')} at {_format_position(item.get('position'))}"
        for item in items[:4]
    )


def _format_spawned_drops(items: list[dict[str, Any]]) -> str:
    """Format causally bounded item entities observed after one action."""

    if not items:
        return "none"
    return ", ".join(
        f"{item.get('item')} x{item.get('count')}" for item in items[:4]
    )


def _format_threat_pause(value: Any) -> str:
    """Format threat-pause state for the model-facing state summary."""

    if not isinstance(value, dict):
        return ""
    threats = _list_of_dicts(value.get("threats"))
    if not value.get("should_pause") or not threats:
        return "No hostile threat pause is active."
    names = ", ".join(
        f"{threat.get('name')} at distance {threat.get('distance')}" for threat in threats[:3]
    )
    return f"World is frozen for hostile-entity deliberation. Nearby threats: {names}."


def _format_counts(counts: dict[str, int]) -> str:
    """Format count dictionaries for model-readable summaries."""

    if not counts:
        return "empty"
    return ", ".join(f"{name} x{count}" for name, count in counts.items())


def _round_number(value: Any) -> float | None:
    """Round numeric values to keep prompt evidence stable and compact."""

    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    """Return a dictionary or an empty dictionary."""

    return value if isinstance(value, dict) else {}


def _list_of_strings(value: Any) -> list[str]:
    """Return a list containing only string elements."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    """Return a list containing only dictionary elements."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
