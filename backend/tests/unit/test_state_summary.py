import pytest

from mc_agent_harness.harness.state_summary import build_state_context


BASE_OBSERVATION = {
    "position": {"x": 0.1868727656, "y": 64, "z": 9.138927957},
    "health": 20,
    "food": 20,
    "inventory": [{"name": "oak_log", "count": 1}],
    "equipment": {
        "main_hand": {"name": "iron_sword", "count": 1},
        "off_hand": {"name": "shield", "count": 1},
        "head": None,
        "chest": None,
        "legs": None,
        "feet": None,
    },
    "nearby_blocks": [{"name": "oak_log", "position": {"x": 0, "y": 64, "z": 10}, "distance": 1.2}],
    "nearby_entities": [
        {"id": 1, "name": "zombie", "type": "mob", "position": {"x": 3, "y": 64, "z": 3}}
    ],
}

TASK_SPEC = {
    "task_id": "minedojo_harvest_oak_log",
    "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
}


@pytest.mark.parametrize(
    ("action_type", "args", "result", "expected"),
    [
        (
            "scan_blocks",
            {"block": "oak_log"},
            {
                "ok": True,
                "action_type": "scan_blocks",
                "query": "oak_log",
                "blocks": [
                    {
                        "name": "oak_log",
                        "position": {"x": 0, "y": 64, "z": 10},
                        "distance": 5.244,
                        "can_dig": False,
                    }
                ],
            },
            {"found_count": 1, "nearest_targets": [{"name": "oak_log"}]},
        ),
        (
            "move_to",
            {"position": {"x": 1, "y": 65, "z": 0}, "tolerance": 1.5},
            {
                "ok": False,
                "action_type": "move_to",
                "error_code": "timeout",
                "target": {"x": 1, "y": 65, "z": 0},
                "tolerance": 1.5,
                "observation": BASE_OBSERVATION,
            },
            {"target": {"x": 1.0, "y": 65.0, "z": 0.0}, "blockers": ["timeout"]},
        ),
        (
            "follow",
            {"entity_id": 143, "follow_distance": 1.25},
            {
                "ok": True,
                "action_type": "follow",
                "status": "following",
                "persistent": True,
                "until": "next_action_received",
                "follow_distance": 1.25,
                "target": {
                    "id": 143,
                    "name": "sheep",
                    "type": "animal",
                    "position": {"x": 20, "y": 70, "z": 249},
                    "distance": 8.4,
                },
                "active_follow": {
                    "active": True,
                    "target": {
                        "id": 143,
                        "name": "sheep",
                        "type": "animal",
                        "position": {"x": 20, "y": 70, "z": 249},
                    },
                    "follow_distance": 1.25,
                    "until": "next_action_received",
                },
                "recommended_next_actions": [
                    (
                        "use_item: Use when the task requires using the held item "
                        "on the followed entity."
                    ),
                    (
                        "move_to_and_engage_combat: Use when the task requires "
                        "attacking the followed entity."
                    ),
                ],
            },
            {
                "status": "following",
                "follow_distance": 1.25,
                "persistent": True,
                "until": "next_action_received",
                "target": {"name": "sheep"},
                "active_follow": {"active": True},
                "recommended_next_actions": [
                    (
                        "use_item: Use when the task requires using the held item "
                        "on the followed entity."
                    ),
                    (
                        "move_to_and_engage_combat: Use when the task requires "
                        "attacking the followed entity."
                    ),
                ],
            },
        ),
        (
            "dig_block_at",
            {"position": {"x": 0, "y": 64, "z": 10}, "block": "oak_log"},
            {
                "ok": True,
                "action_type": "dig_block_at",
                "position": {"x": 0, "y": 64, "z": 10},
                "block_before": "oak_log",
                "block_after": "air",
                "inventory_delta": {},
                "spawned_drops": [
                    {
                        "entity_id": 42,
                        "item": "oak_log",
                        "count": 1,
                        "position": {"x": 0.5, "y": 64.2, "z": 10.5},
                    }
                ],
                "drop_observation_status": "drop_entity_observed",
                "drop_evidence_source": "minecraft_server_entity_packets_and_inventory",
            },
            {
                "world_delta": {"before": "oak_log", "after": "air"},
                "drop_observation_status": "drop_entity_observed",
                "spawned_drops": [{"item": "oak_log", "count": 1}],
            },
        ),
        (
            "wait_ticks",
            {"ticks": 20},
            {
                "ok": True,
                "action_type": "wait_ticks",
                "waited_ticks": 20,
                "inventory_delta": {"oak_log": 1},
                "observation": BASE_OBSERVATION,
            },
            {"inventory_delta": {"oak_log": 1}, "inventory_counts": {"oak_log": 1}},
        ),
        (
            "query_inventory",
            {},
            {
                "ok": True,
                "action_type": "query_inventory",
                "inventory": [{"name": "oak_log", "count": 1}],
            },
            {"inventory_counts": {"oak_log": 1}},
        ),
        (
            "craft_item",
            {"item": "oak_planks", "count": 4},
            {
                "ok": True,
                "action_type": "craft_item",
                "item": "oak_planks",
                "count": 4,
                "craft_count": 1,
                "produced_per_craft": 4,
                "expected_output_count": 4,
                "station": "inventory",
            },
            {"item": "oak_planks", "expected_output_count": 4},
        ),
        (
            "process_item",
            {"station": "furnace", "output": "glass", "input": "sand", "fuel": "coal", "count": 1},
            {
                "ok": True,
                "action_type": "process_item",
                "station": "furnace",
                "item": "glass",
                "input": "sand",
                "fuel": "coal",
                "count": 1,
                "output_count": 1,
                "inventory_delta": {"glass": 1},
            },
            {"station": "furnace", "item": "glass", "output_count": 1},
        ),
        (
            "place_block",
            {"item": "crafting_table"},
            {
                "ok": True,
                "action_type": "place_block",
                "item": "crafting_table",
                "target": {"x": 2, "y": 64, "z": 2},
                "reference": {"x": 2, "y": 63, "z": 2},
            },
            {"item": "crafting_table", "target": {"x": 2.0, "y": 64.0, "z": 2.0}},
        ),
        (
            "use_item",
            {"item": "water_bucket", "block": "lava"},
            {
                "ok": True,
                "action_type": "use_item",
                "activated": "block",
                "block": "lava",
                "item": "water_bucket",
            },
            {"activated": "block", "target": "lava"},
        ),
        (
            "use_item",
            {"entity_id": 68},
            {
                "ok": True,
                "action_type": "use_item",
                "activated": "entity",
                "entity_id": 68,
                "entity": "sheep",
                "item": "shears",
                "held_item": "shears",
                "inventory_delta": {"brown_wool": 3},
                "spawned_drops": [
                    {
                        "entity_id": 541,
                        "item": "brown_wool",
                        "count": 1,
                        "position": {"x": 9.8, "y": 67, "z": 161.8},
                    }
                ],
                "metadata_delta": {"wool": {"before": 0, "after": 16}},
                "target_details_before": {
                    "source": "minecraft_server_entity_packets_and_versioned_registry",
                    "entity_type_id": 82,
                    "registry_name": "sheep",
                    "registry_category": "Passive mobs",
                    "metadata_available": True,
                    "metadata": {
                        "shared_flags": 0,
                        "health": 8,
                        "baby": False,
                        "wool": 0,
                    },
                },
                "target_details_after": {
                    "source": "minecraft_server_entity_packets_and_versioned_registry",
                    "entity_type_id": 82,
                    "registry_name": "sheep",
                    "registry_category": "Passive mobs",
                    "metadata_available": True,
                    "metadata": {
                        "shared_flags": 0,
                        "health": 8,
                        "baby": False,
                        "wool": 16,
                    },
                },
                "effect_observation_ms": 750,
                "observed_effect": True,
                "effect_evidence_source": (
                    "minecraft_server_entity_packets_metadata_and_inventory"
                ),
            },
            {
                "activated": "entity",
                "entity_id": 68,
                "held_item": "shears",
                "inventory_delta": {"brown_wool": 3},
                "spawned_drops": [{"item": "brown_wool"}],
                "metadata_delta": {"wool": {"before": 0, "after": 16}},
                "target_details_after": {
                    "entity_type_id": 82,
                    "metadata": {"baby": False, "wool": 16},
                },
                "observed_effect": True,
            },
        ),
        (
            "equip_item",
            {"item": "bow", "slot": "hand"},
            {
                "ok": True,
                "action_type": "equip_item",
                "item": "bow",
                "slot": "hand",
                "equipment_before": {"main_hand": {"name": "iron_sword", "count": 1}},
                "equipment_after": {"main_hand": {"name": "bow", "count": 1}},
            },
            {
                "item": "bow",
                "slot": "hand",
                "equipment_after": {"main_hand": {"name": "bow", "count": 1}},
            },
        ),
        (
            "scan_entities",
            {"entity": "phantom"},
            {
                "ok": True,
                "action_type": "scan_entities",
                "query": "phantom",
                "entities": [
                    {
                        "entity_id": 12,
                        "id": 12,
                        "name": "phantom",
                        "type": "mob",
                        "position": {"x": 4, "y": 70, "z": 2},
                        "distance": 8.25,
                        "height_delta": 6,
                        "line_of_sight": True,
                        "target_airborne": True,
                        "melee_reachable": False,
                        "suggested_modes": ["ranged", "melee"],
                        "details": {
                            "source": (
                                "minecraft_server_entity_packets_and_versioned_registry"
                            ),
                            "entity_type_id": 82,
                            "registry_name": "phantom",
                            "registry_type": "hostile",
                            "registry_category": "Hostile mobs",
                            "metadata_available": True,
                            "metadata": {
                                "shared_flags": 0,
                                "health": 20,
                                "size": 3,
                            },
                        },
                    }
                ],
            },
            {
                "found_count": 1,
                "nearest_entities": [
                    {
                        "entity_id": 12,
                        "id": 12,
                        "name": "phantom",
                        "target_airborne": True,
                        "suggested_modes": ["ranged", "melee"],
                        "details": {
                            "entity_type_id": 82,
                            "registry_category": "Hostile mobs",
                            "metadata": {"size": 3, "health": 20},
                        },
                    }
                ],
            },
        ),
        (
            "consume_item",
            {"item": "cooked_beef"},
            {
                "ok": True,
                "action_type": "consume_item",
                "item": "cooked_beef",
                "health_before": 8,
                "health_after": 12,
                "health_delta": 4,
                "food_before": 10,
                "food_after": 18,
                "food_delta": 8,
                "inventory_delta": {"cooked_beef": -1},
            },
            {"item": "cooked_beef", "health_delta": 4.0, "food_delta": 8.0},
        ),
        (
            "move_to_and_engage_combat",
            {"entity": "phantom", "mode": "melee"},
            {
                "ok": False,
                "action_type": "move_to_and_engage_combat",
                "entity": "phantom",
                "mode": "melee",
                "status": "target_unreachable",
                "state_summary": "Melee engagement cannot reach the airborne target.",
                "reachability_scope": "current_engagement",
                "tracking_duration_ms": 8000,
                "unreachable_timeout_ms": 8000,
                "stalled_for_ms": 8000,
                "initial_distance": 12.0,
                "closest_distance": 8.25,
                "final_distance": 8.25,
                "distance_progress": 3.75,
                "initial_height_delta": 5.0,
                "final_height_delta": 4.0,
                "follow_updates": 8,
                "diagnosis": "Target remained outside melee range during this engagement.",
                "recovery_guidance": ["Re-scan because the target may land."],
                "target": {
                    "name": "phantom",
                    "position": {"x": 4, "y": 70, "z": 2},
                    "distance": 8.25,
                    "target_airborne": True,
                    "suggested_modes": ["ranged"],
                },
                "suggested_modes": ["ranged"],
                "suggested_next_actions": ["query_inventory", "engage_combat"],
            },
            {
                "entity": "phantom",
                "mode": "melee",
                "status": "target_unreachable",
                "reachability_scope": "current_engagement",
                "closest_distance": 8.25,
                "distance_progress": 3.75,
                "follow_updates": 8,
                "target": {"name": "phantom", "target_airborne": True},
                "suggested_modes": ["ranged"],
            },
        ),
        (
            "fight_entity",
            {"entity": "zombie", "weapon": "wooden_sword"},
            {
                "ok": True,
                "action_type": "fight_entity",
                "entity": "zombie",
                "attacks": 4,
                "defeated": True,
            },
            {"entity": "zombie", "attacks": 4, "defeated": True},
        ),
        (
            "request_visual_snapshot",
            {},
            {
                "ok": True,
                "action_type": "request_visual_snapshot",
                "snapshot": {"image": None, "format": None, "reason": "not configured"},
            },
            {"snapshot_available": False, "reason": "not configured"},
        ),
        (
            "execute_skill",
            {"name": "collect_wood", "version": "0.1.0"},
            {
                "ok": False,
                "action_type": "execute_skill",
                "name": "collect_wood",
                "version": "0.1.0",
                "failed_step": 2,
            },
            {"name": "collect_wood", "version": "0.1.0", "failed_step": 2},
        ),
    ],
)
def test_state_summary_compresses_each_action_type(action_type, args, result, expected) -> None:
    previous_step = {
        "step_index": 3,
        "action": {"type": action_type, "args": args},
        "action_result": result,
    }

    context = build_state_context(TASK_SPEC, BASE_OBSERVATION, previous_step)
    evidence = context["compact_evidence"]["previous_step"]

    assert context["state_summary"]
    assert evidence["action_type"] == action_type
    _assert_subset(expected, evidence)


def test_state_summary_includes_goal_progress_and_current_state() -> None:
    context = build_state_context(TASK_SPEC, BASE_OBSERVATION, None)

    assert context["task_objective"]["goal"] == TASK_SPEC["task_id"]
    assert context["task_objective"]["verifier"] == TASK_SPEC["verifier"]
    assert context["task_progress"]["completion_status"] == (
        "goal_satisfied_by_current_observation"
    )
    assert context["task_progress"]["goal"] == TASK_SPEC["task_id"]
    assert f"Task goal: {TASK_SPEC['task_id']}." in context["state_summary"]
    assert "oak_log 1/1" in context["state_summary"]
    assert "Equipment: main_hand=iron_sword, off_hand=shield" in context["state_summary"]
    assert context["compact_evidence"]["goal_progress"][0]["satisfied"] is True
    assert context["compact_evidence"]["current_state"]["position"] == {
        "x": 0.19,
        "y": 64.0,
        "z": 9.14,
    }
    assert context["compact_evidence"]["current_state"]["equipment"]["main_hand"] == {
        "name": "iron_sword",
        "count": 1,
    }


def test_empty_entity_scan_recommends_relocation_before_rescan() -> None:
    previous_step = {
        "step_index": 4,
        "action": {
            "type": "scan_entities",
            "args": {"entity": "sheep", "max_distance": 128, "count": 20},
        },
        "action_result": {
            "ok": True,
            "action_type": "scan_entities",
            "query": "sheep",
            "max_distance": 128,
            "entities": [],
        },
    }

    context = build_state_context(TASK_SPEC, BASE_OBSERVATION, previous_step)
    evidence = context["compact_evidence"]["previous_step"]

    assert "currently loaded area" in evidence["exploration_hint"]
    assert "memory rules out every candidate" in evidence["exploration_hint"]
    assert "move to a different reachable area tens of blocks away" in (
        evidence["exploration_hint"]
    )
    assert "Do not only increase count or max_distance" in evidence["summary"]


def test_state_summary_keeps_observed_drops_separate_from_verifier_target() -> None:
    """Non-target drops remain environment evidence and never stand in for the task goal."""

    observation = {
        **BASE_OBSERVATION,
        "inventory": [{"name": "shears", "count": 1}],
        "nearby_entities": [
            {
                "entity_id": 541,
                "id": 541,
                "name": "item",
                "type": "other",
                "dropped_item": {"name": "brown_wool", "count": 1},
                "position": {"x": 1, "y": 64, "z": 1},
                "distance": 1.4,
            },
            {
                "entity_id": 68,
                "id": 68,
                "name": "sheep",
                "type": "animal",
                "position": {"x": 2, "y": 64, "z": 2},
                "distance": 2.8,
            },
        ],
    }
    context = build_state_context(
        {
            "task_id": "harvest_white_wool",
            "goal": "Obtain one white wool by shearing a sheep.",
            "verifier": {
                "type": "inventory_delta_contains",
                "item": "white_wool",
                "count": 1,
            },
            "require_inventory_delta": True,
            "_initial_inventory": [],
        },
        observation,
        None,
    )

    state = context["compact_evidence"]["current_state"]
    assert state["observed_dropped_items"] == [
        {
            "entity_id": 541,
            "item": "brown_wool",
            "count": 1,
            "position": {"x": 1.0, "y": 64.0, "z": 1.0},
            "distance": 1.4,
        }
    ]
    assert context["task_progress"]["checks"][0]["item"] == "white_wool"
    assert "Observed dropped item entities: brown_wool x1" in context["state_summary"]
    assert "Nearby dropped items" not in context["state_summary"]


def test_state_summary_exposes_threat_pause_observation() -> None:
    observation = {
        **BASE_OBSERVATION,
        "threat_pause": {
            "should_pause": True,
            "already_paused": False,
            "threats": [
                {
                    "id": 2,
                    "name": "skeleton",
                    "type": "skeleton",
                    "distance": 6.4,
                    "position": {"x": 4, "y": 64, "z": 5},
                    "line_of_sight": True,
                    "target_airborne": False,
                }
            ],
            "command_results": [
                {"command": "tick freeze", "ok": True, "response": "Game is frozen"}
            ],
        },
    }

    context = build_state_context(TASK_SPEC, observation, None)
    threat_pause = context["compact_evidence"]["current_state"]["threat_pause"]

    assert threat_pause["world_frozen_for_model_decision"] is True
    assert threat_pause["threats"][0]["name"] == "skeleton"
    assert "World is frozen for hostile-entity deliberation" in context["state_summary"]


def test_state_summary_marks_live_inventory_delta_targets() -> None:
    context = build_state_context(
        {
            **TASK_SPEC,
            "require_inventory_delta": True,
            "_initial_inventory": [{"name": "oak_log", "count": 1}],
        },
        BASE_OBSERVATION,
        None,
    )

    progress = context["compact_evidence"]["goal_progress"][0]
    assert progress == {
        "type": "inventory_delta_contains",
        "item": "oak_log",
        "initial_count": 1,
        "current_delta": 0,
        "inventory_count": 1,
        "target_delta": 1,
        "satisfied": False,
    }
    assert "new oak_log +0/+1" in context["state_summary"]
    assert "pre-task items do not count" in context["state_summary"]


def test_state_summary_counts_live_inventory_delta_from_initial_inventory() -> None:
    context = build_state_context(
        {**TASK_SPEC, "require_inventory_delta": True, "_initial_inventory": []},
        BASE_OBSERVATION,
        None,
    )

    progress = context["compact_evidence"]["goal_progress"][0]
    assert progress["current_delta"] == 1
    assert progress["satisfied"] is True
    assert "new oak_log +1/+1" in context["state_summary"]
    assert "initial 0" in context["state_summary"]


def test_state_summary_preserves_move_to_nearest_reachable_position() -> None:
    context = build_state_context(
        TASK_SPEC,
        BASE_OBSERVATION,
        {
            "step_index": 4,
            "action": {
                "type": "move_to",
                "args": {"position": {"x": 3, "y": 68, "z": 4}},
            },
            "action_result": {
                "ok": False,
                "action_type": "move_to",
                "error_code": "no_path",
                "target": {"x": 3, "y": 68, "z": 4},
                "nearest_reachable_position": {"x": 3, "y": 65, "z": 4},
                "target_height_delta": 3,
                "state_summary": "The target is not reachable.",
                "observation": BASE_OBSERVATION,
            },
        },
    )

    previous = context["compact_evidence"]["previous_step"]
    assert previous["nearest_reachable_position"] == {"x": 3.0, "y": 65.0, "z": 4.0}
    assert previous["target_height_delta"] == 3.0
    assert "Nearest reachable position: (3.0,65.0,4.0)" in previous["summary"]


def test_state_summary_suggests_scaffold_materials_after_non_diggable_scan() -> None:
    context = build_state_context(
        TASK_SPEC,
        BASE_OBSERVATION,
        {
            "step_index": 2,
            "action": {"type": "scan_blocks", "args": {"block": "oak_log"}},
            "action_result": {
                "ok": True,
                "action_type": "scan_blocks",
                "query": "oak_log",
                "blocks": [
                    {
                        "name": "oak_log",
                        "position": {"x": 4, "y": 68, "z": 4},
                        "distance": 7.5,
                        "can_dig": False,
                    }
                ],
            },
        },
    )

    previous = context["compact_evidence"]["previous_step"]
    assert "safe scaffold blocks" in previous["summary"]
    assert "query_inventory" in previous["navigation_preparation_hint"]
    assert "dirt" in previous["navigation_preparation_hint"]


def test_state_summary_preserves_move_to_scaffold_failure_reason() -> None:
    context = build_state_context(
        TASK_SPEC,
        BASE_OBSERVATION,
        {
            "step_index": 5,
            "action": {
                "type": "move_to",
                "args": {"position": {"x": 8, "y": 70, "z": 8}},
            },
            "action_result": {
                "ok": False,
                "action_type": "move_to",
                "error_code": "no_path",
                "target": {"x": 8, "y": 70, "z": 8},
                "available_scaffolding_count": 0,
                "scaffolding_item_names": ["cobblestone", "dirt"],
                "navigation_failure_reason": (
                    "Pathfinder needed scaffold blocks but available safe scaffold count is 0. "
                    "Check inventory; gather expendable blocks such as dirt before retrying move_to."
                ),
                "path_resets": ["no_scaffolding_blocks"],
                "observation": BASE_OBSERVATION,
            },
        },
    )

    previous = context["compact_evidence"]["previous_step"]
    assert previous["available_scaffolding_count"] == 0
    assert previous["navigation_failure_reason"].startswith("Pathfinder needed scaffold blocks")
    assert "Navigation failure reason: Pathfinder needed scaffold blocks" in previous["summary"]


def test_state_summary_preserves_move_to_inventory_consumption() -> None:
    context = build_state_context(
        TASK_SPEC,
        BASE_OBSERVATION,
        {
            "step_index": 6,
            "action": {
                "type": "move_to",
                "args": {"position": {"x": 5, "y": 66, "z": 5}},
            },
            "action_result": {
                "ok": True,
                "action_type": "move_to",
                "target": {"x": 5, "y": 66, "z": 5},
                "distance": 0.8,
                "inventory_delta": {"dirt": -2},
                "consumed_items": {"dirt": 2},
                "scaffolding_delta": {"dirt": -2},
                "scaffolding_consumed": {"dirt": 2},
                "observation": BASE_OBSERVATION,
            },
        },
    )

    previous = context["compact_evidence"]["previous_step"]
    assert previous["inventory_delta"] == {"dirt": -2}
    assert previous["scaffolding_consumed"] == {"dirt": 2}
    assert "Inventory delta from move_to: -2 dirt" in previous["summary"]
    assert "Pathfinder consumed scaffold blocks: dirt x2" in previous["summary"]


def test_state_summary_preserves_rejected_finish_evidence() -> None:
    """A rejected finish request should tell the next ReAct turn why it must continue."""

    context = build_state_context(
        TASK_SPEC,
        BASE_OBSERVATION,
        {
            "step_index": 7,
            "action": {"type": "submit_for_evaluation", "args": {}},
            "action_result": {
                "ok": False,
                "action_type": "submit_for_evaluation",
                "submission_accepted": False,
                "evaluation_status": "rejected",
                "task_success": False,
                "error_code": "submission_rejected",
                "summary": "Finish request rejected. Continue from verifier evidence: target missing.",
                "verifier": {"success": False, "reason": "target missing"},
            },
        },
    )

    previous = context["compact_evidence"]["previous_step"]
    assert previous["submission_accepted"] is False
    assert previous["verifier_reason"] == "target missing"
    assert previous["blockers"] == ["submission_rejected"]
    assert "Continue from verifier evidence" in previous["summary"]


def test_state_summary_exposes_mineclip_progress_as_advisory_only() -> None:
    """Online score trends reach the model without being represented as success evidence."""

    observation = {
        **BASE_OBSERVATION,
        "creative_progress": {
            "latest": {
                "job_id": "mineclip-progress-1",
                "status": "completed",
                "action_type": "place_block",
                "score": 0.61,
                "score_delta": 0.07,
                "trend": "improving",
                "confidence": "low",
                "advisory_only": True,
                "success_authority": "human_review",
                "summary": (
                    "MineCLIP advisory after place_block: alignment 0.6100, delta +0.0700, "
                    "trend improving. This is not proof of task completion."
                ),
            },
            "pending_jobs": 0,
            "buffer_ready": True,
        },
    }

    context = build_state_context(TASK_SPEC, observation, None)
    progress = context["compact_evidence"]["current_state"]["creative_progress"]

    assert progress["latest"]["trend"] == "improving"
    assert progress["latest"]["advisory_only"] is True
    assert progress["latest"]["success_authority"] == "human_review"
    assert "not proof of task completion" in context["state_summary"]


def _assert_subset(expected, actual) -> None:
    """Assert nested expected keys are present in the actual dictionary."""

    for key, value in expected.items():
        assert key in actual
        if isinstance(value, dict):
            _assert_subset(value, actual[key])
        elif isinstance(value, list):
            assert actual[key]
            if isinstance(value[0], dict):
                _assert_subset(value[0], actual[key][0])
            else:
                assert value[0] in actual[key]
        else:
            assert actual[key] == value
