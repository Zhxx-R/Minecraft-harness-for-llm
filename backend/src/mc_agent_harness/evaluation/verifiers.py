from __future__ import annotations

from typing import Any


class ProgrammaticVerifier:
    """Programmatic success checker for deterministic Minecraft task manifests."""

    async def verify(self, task_spec: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
        """Evaluate the run state against one verifier specification."""

        verifier = task_spec.get("verifier") or task_spec.get("success_criteria")
        if verifier is None:
            return {"success": False, "reason": "No verifier specified.", "checks": []}

        result = _verify_spec(verifier, run_state)
        response = {
            "success": result["success"],
            "reason": result["reason"],
            "checks": result.get("checks", [result]),
        }
        if result.get("inconclusive") is True:
            response["inconclusive"] = True
        return response


def _verify_spec(verifier: Any, run_state: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one verifier object or composite verifier."""

    if isinstance(verifier, list):
        return _verify_all(verifier, run_state)
    if not isinstance(verifier, dict):
        return _check(False, "invalid", "Verifier must be an object or list.")
    if "all" in verifier:
        return _verify_all(verifier["all"], run_state)
    if "any" in verifier:
        return _verify_any(verifier["any"], run_state)

    verifier_type = str(verifier.get("type", ""))
    if verifier_type == "inventory_contains":
        return _verify_inventory_contains(verifier, run_state)
    if verifier_type == "block_placed":
        return _verify_block_placed(verifier, run_state)
    if verifier_type == "entity_defeated":
        return _verify_entity_defeated(verifier, run_state)
    if verifier_type == "entity_kill_delta":
        return _verify_entity_kill_delta(verifier, run_state)
    if verifier_type == "item_used_delta":
        return _verify_item_used_delta(verifier, run_state)
    if verifier_type == "time_alive":
        return _verify_time_alive(verifier, run_state)
    if verifier_type == "creative_mineclip":
        return _check(
            False,
            "creative_mineclip",
            "Creative tasks require external MineCLIP frame evaluation after agent execution.",
            {
                "inconclusive": True,
                "external_evaluator": "mineclip",
                "evaluator_visibility": "external_not_agent_context",
            },
        ) | {"inconclusive": True}
    return _check(False, verifier_type or "unknown", f"Unknown verifier type: {verifier_type}.")


def _verify_all(verifiers: Any, run_state: dict[str, Any]) -> dict[str, Any]:
    """Require every nested verifier to pass."""

    if not isinstance(verifiers, list):
        return _check(False, "all", "Composite verifier 'all' must be a list.")
    checks = [_verify_spec(item, run_state) for item in verifiers]
    success = all(check["success"] for check in checks)
    reason = "All verifier checks passed." if success else "At least one verifier check failed."
    return {"type": "all", "success": success, "reason": reason, "checks": checks}


def _verify_any(verifiers: Any, run_state: dict[str, Any]) -> dict[str, Any]:
    """Require at least one nested verifier to pass."""

    if not isinstance(verifiers, list):
        return _check(False, "any", "Composite verifier 'any' must be a list.")
    checks = [_verify_spec(item, run_state) for item in verifiers]
    success = any(check["success"] for check in checks)
    reason = "At least one verifier check passed." if success else "No verifier checks passed."
    return {"type": "any", "success": success, "reason": reason, "checks": checks}


def _verify_inventory_contains(verifier: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
    """Check whether the latest inventory contains an item count."""

    item_id = _expected_name(verifier, "item")
    expected_count = int(verifier.get("count", 1))
    if item_id is None:
        return _check(False, "inventory_contains", "inventory_contains requires item/item_id/name.")

    inventory = _latest_inventory(run_state)
    actual_count = sum(int(item.get("count", 0)) for item in inventory if item.get("name") == item_id)
    if _requires_inventory_delta(verifier, run_state):
        initial_count = _inventory_count(_initial_inventory(run_state), item_id)
        actual_delta = actual_count - initial_count
        success = actual_delta >= expected_count
        return _check(
            success,
            "inventory_delta_contains",
            (
                f"Inventory delta is {actual_delta:+d} {item_id}, expected at least +{expected_count} "
                f"(initial {initial_count}, latest {actual_count})."
            ),
            {
                "item": item_id,
                "initial_count": initial_count,
                "actual_count": actual_count,
                "actual_delta": actual_delta,
                "expected_delta": expected_count,
            },
        )

    if actual_count >= expected_count:
        return _check(
            True,
            "inventory_contains",
            f"Inventory contains {actual_count} {item_id}, expected at least {expected_count}.",
            {"item": item_id, "actual_count": actual_count, "expected_count": expected_count},
        )
    return _check(
        False,
        "inventory_contains",
        f"Inventory contains {actual_count} {item_id}, expected at least {expected_count}.",
        {"item": item_id, "actual_count": actual_count, "expected_count": expected_count},
    )


def _verify_block_placed(verifier: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
    """Check whether a block appears in the latest observation or placement result."""

    block_id = _expected_name(verifier, "block")
    if block_id is None:
        return _check(False, "block_placed", "block_placed requires block/block_id/name.")

    action_result = _latest_action_result(run_state)
    if (
        action_result.get("ok") is True
        and action_result.get("action_type") == "place_block"
        and action_result.get("item") == block_id
        and _position_matches(verifier.get("position"), action_result.get("target"))
    ):
        return _check(True, "block_placed", f"Action result placed {block_id}.", {"block": block_id})

    for block in _latest_blocks(run_state):
        if block.get("name") == block_id and _position_matches(verifier.get("position"), block.get("position")):
            return _check(True, "block_placed", f"Observed placed block {block_id}.", {"block": block_id})

    return _check(False, "block_placed", f"Did not observe placed block {block_id}.", {"block": block_id})


def _verify_entity_defeated(verifier: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
    """Check whether an entity is no longer nearby or combat reported defeat."""

    entity_id = _expected_name(verifier, "entity")
    if entity_id is None:
        return _check(False, "entity_defeated", "entity_defeated requires entity/entity_id/name.")

    action_result = _latest_action_result(run_state)
    if (
        action_result.get("ok") is True
        and action_result.get("action_type") == "fight_entity"
        and action_result.get("entity") == entity_id
        and action_result.get("defeated") is True
    ):
        return _check(True, "entity_defeated", f"Combat result defeated {entity_id}.", {"entity": entity_id})
    if (
        action_result.get("ok") is True
        and action_result.get("action_type")
        in {"move_to_and_engage_combat", "engage_combat"}
        and action_result.get("entity") == entity_id
        and action_result.get("status") == "target_killed"
    ):
        return _check(True, "entity_defeated", f"Bounded combat killed {entity_id}.", {"entity": entity_id})

    still_present = any(_entity_matches(entity, entity_id) for entity in _latest_entities(run_state))
    if not still_present:
        return _check(True, "entity_defeated", f"Entity {entity_id} is not present in latest observation.", {"entity": entity_id})
    return _check(False, "entity_defeated", f"Entity {entity_id} is still present.", {"entity": entity_id})


def _verify_entity_kill_delta(verifier: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
    """Check native or worker-confirmed entity-death counters without using item drops."""

    entity_id = _expected_name(verifier, "entity")
    expected_count = int(verifier.get("count", 1))
    if entity_id is None:
        return _check(False, "entity_kill_delta", "entity_kill_delta requires entity/entity_id/name.")
    initial_stats = _initial_stats(run_state)
    latest_stats = _latest_stats(run_state)
    initial_compatibility_count = _stat_count(initial_stats, "kill_entity", entity_id)
    latest_compatibility_count = _stat_count(latest_stats, "kill_entity", entity_id)
    compatibility_delta = latest_compatibility_count - initial_compatibility_count
    initial_native_count = _stat_count(initial_stats, "native_kill_entity", entity_id)
    latest_native_count = _stat_count(latest_stats, "native_kill_entity", entity_id)
    native_delta = latest_native_count - initial_native_count
    initial_confirmed_count = _stat_count(initial_stats, "confirmed_kill_entity", entity_id)
    latest_confirmed_count = _stat_count(latest_stats, "confirmed_kill_entity", entity_id)
    confirmed_delta = latest_confirmed_count - initial_confirmed_count
    delta = max(native_delta, confirmed_delta, compatibility_delta)
    if confirmed_delta == delta and confirmed_delta > 0:
        source = str(latest_stats.get("kill_count_source") or "mineflayer_entity_dead")
        effective_initial_count = initial_confirmed_count
        effective_latest_count = latest_confirmed_count
    elif native_delta == delta and native_delta > 0:
        source = "native_kill_stat"
        effective_initial_count = initial_native_count
        effective_latest_count = latest_native_count
    elif compatibility_delta == delta and compatibility_delta > 0:
        source = "kill_entity_compat"
        effective_initial_count = initial_compatibility_count
        effective_latest_count = latest_compatibility_count
    else:
        source = str(latest_stats.get("kill_count_source") or "no_kill_evidence")
        effective_initial_count = initial_confirmed_count
        effective_latest_count = latest_confirmed_count
    return _check(
        delta >= expected_count,
        "entity_kill_delta",
        (
            f"Confirmed kill delta is {delta:+d} {entity_id}, expected at least +{expected_count} "
            f"(source {source}; native {native_delta:+d}, worker-confirmed {confirmed_delta:+d}, "
            f"compatibility {compatibility_delta:+d})."
        ),
        {
            "entity": entity_id,
            "initial_count": effective_initial_count,
            "actual_count": effective_latest_count,
            "actual_delta": delta,
            "expected_delta": expected_count,
            "native_delta": native_delta,
            "confirmed_event_delta": confirmed_delta,
            "compatibility_delta": compatibility_delta,
            "native_initial_count": initial_native_count,
            "native_actual_count": latest_native_count,
            "confirmed_initial_count": initial_confirmed_count,
            "confirmed_actual_count": latest_confirmed_count,
            "compatibility_initial_count": initial_compatibility_count,
            "compatibility_actual_count": latest_compatibility_count,
            "kill_count_source": source,
        },
    )


def _verify_item_used_delta(verifier: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
    """Check whether use_item statistics increased for a target item."""

    item_id = _expected_name(verifier, "item")
    expected_count = int(verifier.get("count", 1))
    if item_id is None:
        return _check(False, "item_used_delta", "item_used_delta requires item/item_id/name.")
    initial_count = _stat_count(_initial_stats(run_state), "use_item", item_id)
    latest_count = _stat_count(_latest_stats(run_state), "use_item", item_id)
    delta = latest_count - initial_count
    return _check(
        delta >= expected_count,
        "item_used_delta",
        (
            f"Use-item stat delta is {delta:+d} {item_id}, expected at least +{expected_count} "
            f"(initial {initial_count}, latest {latest_count})."
        ),
        {
            "item": item_id,
            "initial_count": initial_count,
            "actual_count": latest_count,
            "actual_delta": delta,
            "expected_delta": expected_count,
        },
    )


def _verify_time_alive(verifier: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]:
    """Check whether the latest observation reports enough survival ticks."""

    target_days = int(verifier.get("target_days", 1))
    expected_ticks = int(verifier.get("ticks", target_days * 24000))
    initial_ticks = _time_alive_ticks(_initial_observation(run_state))
    latest_ticks = _time_alive_ticks(_latest_observation(run_state))
    delta = max(0, latest_ticks - initial_ticks)
    return _check(
        delta >= expected_ticks,
        "time_alive",
        f"Alive time delta is {delta} ticks, expected at least {expected_ticks}.",
        {
            "initial_ticks": initial_ticks,
            "actual_ticks": latest_ticks,
            "actual_delta": delta,
            "expected_ticks": expected_ticks,
            "target_days": target_days,
        },
    )


def _latest_observation(run_state: dict[str, Any]) -> dict[str, Any]:
    """Extract the most recent observation from a run-state dictionary."""

    direct = run_state.get("latest_observation") or run_state.get("observation")
    if isinstance(direct, dict):
        return direct

    action_result = _latest_action_result(run_state)
    result_observation = action_result.get("observation")
    if isinstance(result_observation, dict):
        return result_observation

    steps = run_state.get("steps")
    if isinstance(steps, list) and steps:
        last_step = steps[-1]
        if isinstance(last_step, dict):
            step_result = last_step.get("action_result")
            if isinstance(step_result, dict) and isinstance(step_result.get("observation"), dict):
                return step_result["observation"]
            if isinstance(last_step.get("observation"), dict):
                return last_step["observation"]
    return {}


def _initial_observation(run_state: dict[str, Any]) -> dict[str, Any]:
    """Extract the first observation from direct state or completed steps."""

    direct = run_state.get("initial_observation")
    if isinstance(direct, dict):
        return direct
    steps = run_state.get("steps")
    if isinstance(steps, list) and steps:
        first_step = steps[0]
        if isinstance(first_step, dict) and isinstance(first_step.get("observation"), dict):
            return first_step["observation"]
    return {}


def _latest_action_result(run_state: dict[str, Any]) -> dict[str, Any]:
    """Extract the most recent action result from a run-state dictionary."""

    direct = run_state.get("latest_action_result") or run_state.get("action_result")
    if isinstance(direct, dict):
        return direct
    steps = run_state.get("steps")
    if isinstance(steps, list) and steps:
        last_step = steps[-1]
        if isinstance(last_step, dict) and isinstance(last_step.get("action_result"), dict):
            return last_step["action_result"]
    return {}


def _latest_inventory(run_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract latest inventory items from observation or action result."""

    action_result = _latest_action_result(run_state)
    if isinstance(action_result.get("inventory"), list):
        return [item for item in action_result["inventory"] if isinstance(item, dict)]
    observation = _latest_observation(run_state)
    if isinstance(observation.get("inventory"), list):
        return [item for item in observation["inventory"] if isinstance(item, dict)]
    return []


def _initial_inventory(run_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the inventory snapshot captured before the first action."""

    direct = run_state.get("initial_inventory")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]

    initial_observation = run_state.get("initial_observation")
    if isinstance(initial_observation, dict) and isinstance(initial_observation.get("inventory"), list):
        return [item for item in initial_observation["inventory"] if isinstance(item, dict)]

    steps = run_state.get("steps")
    if isinstance(steps, list) and steps:
        first_step = steps[0]
        if isinstance(first_step, dict):
            observation = first_step.get("observation")
            if isinstance(observation, dict) and isinstance(observation.get("inventory"), list):
                return [item for item in observation["inventory"] if isinstance(item, dict)]
    return []


def _latest_stats(run_state: dict[str, Any]) -> dict[str, Any]:
    """Extract the latest normalized Minecraft statistics from observation or action result."""

    action_result = _latest_action_result(run_state)
    if isinstance(action_result.get("stats"), dict):
        return action_result["stats"]
    observation = _latest_observation(run_state)
    if isinstance(observation.get("stats"), dict):
        return observation["stats"]
    return {}


def _initial_stats(run_state: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized Minecraft statistics from the first observation."""

    initial = _initial_observation(run_state)
    if isinstance(initial.get("stats"), dict):
        return initial["stats"]
    return {}


def _stat_count(stats: dict[str, Any], category: str, name: str) -> int:
    """Read a stat count from normalized or flat stat payloads."""

    nested = stats.get(category)
    if isinstance(nested, dict):
        for key in _stat_key_candidates(name):
            value = nested.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    for key in _stat_key_candidates(f"{category}.{name}") | _stat_key_candidates(f"{category}/{name}"):
        value = stats.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _stat_key_candidates(name: str) -> set[str]:
    """Return common namespaced and un-namespaced statistic key variants."""

    plain = name.removeprefix("minecraft:")
    return {
        name,
        plain,
        f"minecraft:{plain}",
        plain.replace("minecraft.", ""),
        plain.replace("minecraft/", ""),
    }


def _time_alive_ticks(observation: dict[str, Any]) -> int:
    """Read alive-time ticks from normalized stats or world metadata."""

    stats = observation.get("stats") if isinstance(observation.get("stats"), dict) else {}
    custom = stats.get("custom") if isinstance(stats.get("custom"), dict) else {}
    for key in ("time_since_death", "play_time", "minecraft:time_since_death"):
        value = custom.get(key) or stats.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    world = observation.get("world") if isinstance(observation.get("world"), dict) else {}
    value = world.get("age_ticks") or world.get("time_alive_ticks")
    return int(value) if isinstance(value, (int, float)) else 0


def _inventory_count(inventory: list[dict[str, Any]], item_id: str) -> int:
    """Count one item name inside an inventory list."""

    return sum(int(item.get("count", 0)) for item in inventory if item.get("name") == item_id)


def _requires_inventory_delta(verifier: dict[str, Any], run_state: dict[str, Any]) -> bool:
    """Return whether inventory verification must prove this run created new items."""

    mode = verifier.get("mode") or verifier.get("comparison")
    return bool(
        run_state.get("require_inventory_delta")
        or verifier.get("require_delta")
        or verifier.get("delta")
        or mode in {"delta", "inventory_delta"}
    )


def _latest_blocks(run_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract latest nearby block observations."""

    observation = _latest_observation(run_state)
    if isinstance(observation.get("nearby_blocks"), list):
        return [item for item in observation["nearby_blocks"] if isinstance(item, dict)]
    return []


def _latest_entities(run_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract latest nearby entity observations."""

    observation = _latest_observation(run_state)
    if isinstance(observation.get("nearby_entities"), list):
        return [item for item in observation["nearby_entities"] if isinstance(item, dict)]
    return []


def _expected_name(verifier: dict[str, Any], prefix: str) -> str | None:
    """Read a canonical id or name from a verifier object."""

    value = verifier.get(f"{prefix}_id") or verifier.get(prefix) or verifier.get("name")
    return value if isinstance(value, str) and value else None


def _entity_matches(entity: dict[str, Any], expected: str) -> bool:
    """Return whether an observed entity matches an expected id, name, or type."""

    candidates = {
        str(entity.get("id", "")),
        str(entity.get("name", "")),
        str(entity.get("type", "")),
    }
    return expected in candidates


def _position_matches(expected: Any, actual: Any) -> bool:
    """Return whether an optional expected position matches an actual position."""

    if expected is None:
        return True
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    for axis in ("x", "y", "z"):
        if int(expected.get(axis, 0)) != int(actual.get(axis, 0)):
            return False
    return True


def _check(
    success: bool,
    verifier_type: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one auditable verifier check result."""

    return {"type": verifier_type, "success": success, "reason": reason, **(extra or {})}
