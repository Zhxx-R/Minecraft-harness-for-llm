import pytest

from mc_agent_harness.harness.tool_registry import (
    ACTION_PRIMITIVE_GUIDE,
    DEFAULT_HARNESS_ACTIONS,
    DEFAULT_WEEK5_ACTIONS,
    ToolRegistry,
)
from mc_agent_harness.schemas.action import HarnessAction


def test_tool_registry_accepts_enabled_action() -> None:
    registry = ToolRegistry(["query_inventory"])
    action = HarnessAction(type="query_inventory", args={})

    assert registry.validate(action) is action
    assert registry.enabled_actions == ("query_inventory",)
    assert registry.prompt_visible_actions == ("query_inventory",)


def test_tool_registry_rejects_disabled_action() -> None:
    registry = ToolRegistry(["query_inventory"])

    with pytest.raises(ValueError, match="not enabled"):
        registry.validate(
            HarnessAction(
                type="dig_block_at",
                args={"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}},
            )
        )


def test_week5_default_actions_cover_minecraft_action_expansion() -> None:
    """Default Week 5 scope should expose the canonical primitive Minecraft action set."""

    assert set(DEFAULT_WEEK5_ACTIONS) == {
        "query_inventory",
        "request_visual_snapshot",
        "scan_blocks",
        "scan_entities",
        "scan_dropped_items",
        "move_to",
        "follow",
        "dig_block_at",
        "wait_ticks",
        "process_item",
        "craft_item",
        "smelt_item",
        "place_block",
        "equip_item",
        "use_item",
        "consume_item",
        "move_to_and_engage_combat",
        "engage_combat",
        "fight_entity",
    }


def test_default_harness_actions_include_knowledge_tools() -> None:
    """The default agent-facing harness contract includes read-only knowledge tools."""

    assert {"resolve_terms", "get_recipe", "retrieve_docs"} <= set(DEFAULT_HARNESS_ACTIONS)
    assert set(DEFAULT_WEEK5_ACTIONS) < set(DEFAULT_HARNESS_ACTIONS)


def test_default_harness_actions_expose_audited_finish_control() -> None:
    """The model can request evaluation without sending the control action to Mineflayer."""

    assert "submit_for_evaluation" in DEFAULT_HARNESS_ACTIONS
    guide = ACTION_PRIMITIVE_GUIDE["submit_for_evaluation"]
    assert "does not declare success" in str(guide["purpose"])
    assert "verifier" in str(guide["when_to_use"])


def test_get_recipe_prompt_description_covers_smelting() -> None:
    """Recipe lookup should be presented as item processing, not only crafting."""

    guide = ACTION_PRIMITIVE_GUIDE["get_recipe"]

    assert "furnace smelting" in str(guide["purpose"])
    assert "glass" in str(guide["args"])
    assert "fuel" in str(guide["returns"])
    assert "crafted vs smelted" in str(guide["when_to_use"])


def test_tool_registry_exposes_every_enabled_primitive_to_prompt() -> None:
    """Primitive actions are the complete prompt-facing runtime contract."""

    registry = ToolRegistry(["query_inventory", "scan_blocks", "dig_block_at"])
    action = HarnessAction(
        type="dig_block_at", args={"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}}
    )

    assert registry.validate(action) is action
    assert registry.enabled_actions == ("dig_block_at", "query_inventory", "scan_blocks")
    assert registry.prompt_visible_actions == registry.enabled_actions
    assert [guide["type"] for guide in registry.action_guides()] == [
        "dig_block_at",
        "query_inventory",
        "scan_blocks",
    ]


def test_follow_guide_documents_cross_turn_lifecycle() -> None:
    """The model must know that follow persists only until its next action."""

    scan_guide = ACTION_PRIMITIVE_GUIDE["scan_entities"]
    guide = ACTION_PRIMITIVE_GUIDE["follow"]
    use_item_guide = ACTION_PRIMITIVE_GUIDE["use_item"]

    assert "entities[].entity_id" in str(scan_guide["returns"])
    assert "versioned Minecraft registry" in str(scan_guide["returns"])
    assert "during observation and model reasoning" in str(guide["purpose"])
    assert "entities[].entity_id" in str(guide["args"])
    assert "default 1.25" in str(guide["args"])
    assert "defaults to 128" in str(guide["args"])
    assert "next action" in str(guide["when_to_use"])
    assert "effect_observation_ms" in str(use_item_guide["args"])
    assert "local interaction evidence" in str(use_item_guide["returns"])


def test_scan_entities_guide_requires_relocation_after_unsuitable_results() -> None:
    """A broad entity scan must not be presented as exploration from a fixed position."""

    guide = ACTION_PRIMITIVE_GUIDE["scan_entities"]

    assert "currently loaded" in str(guide["purpose"])
    assert "same position" in str(guide["purpose"])
    assert "task memory rules out every candidate" in str(guide["when_to_use"])
    assert "32-64" in str(guide["when_to_use"])
    assert "only increase count/max_distance" in str(guide["when_to_use"])
    assert "exact entity_id" in str(guide["when_to_use"])


def test_tool_registry_hides_legacy_fight_entity_from_prompt() -> None:
    """New combat prompts should expose bounded combat while keeping old action compatibility."""

    registry = ToolRegistry(
        [
            "fight_entity",
            "engage_combat",
            "move_to_and_engage_combat",
            "consume_item",
            "scan_entities",
        ]
    )

    assert registry.enabled_actions == (
        "consume_item",
        "engage_combat",
        "fight_entity",
        "move_to_and_engage_combat",
        "scan_entities",
    )
    assert registry.prompt_visible_actions == (
        "consume_item",
        "move_to_and_engage_combat",
        "scan_entities",
    )
    assert [guide["type"] for guide in registry.action_guides()] == [
        "consume_item",
        "move_to_and_engage_combat",
        "scan_entities",
    ]


def test_move_to_and_engage_combat_contract_explains_tracking_without_automatic_equipment() -> None:
    """Combat semantics should expose dynamic tracking while preserving explicit gear decisions."""

    guide = ACTION_PRIMITIVE_GUIDE["move_to_and_engage_combat"]

    assert "dynamically track" in str(guide["purpose"])
    assert "unreachable_timeout_ms" in guide["args"]
    assert "does not equip weapons" in str(guide["when_to_use"])
    assert "equip_item" in str(guide["when_to_use"])


def test_tool_registry_hides_legacy_processing_aliases_from_prompt() -> None:
    """Process prompts should expose process_item while keeping old aliases callable."""

    registry = ToolRegistry(["process_item", "craft_item", "smelt_item", "query_inventory"])

    assert registry.enabled_actions == (
        "craft_item",
        "process_item",
        "query_inventory",
        "smelt_item",
    )
    assert registry.prompt_visible_actions == ("process_item", "query_inventory")
    assert [guide["type"] for guide in registry.action_guides()] == [
        "process_item",
        "query_inventory",
    ]
