import pytest

from mc_agent_harness.evaluation.verifiers import ProgrammaticVerifier


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_inventory_contains_verifier_reads_latest_observation() -> None:
    """Inventory verifier should count matching canonical item names."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "inventory_contains", "item": "oak_log", "count": 2}},
        {"latest_observation": {"inventory": [{"name": "oak_log", "count": 3}]}},
    )

    assert result["success"] is True
    assert result["checks"][0]["actual_count"] == 3


@pytest.mark.anyio
async def test_inventory_delta_verifier_rejects_preexisting_items() -> None:
    """Live training should not pass when the target item existed before the run."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "inventory_contains", "item": "dirt", "count": 1}},
        {
            "require_inventory_delta": True,
            "initial_inventory": [{"name": "dirt", "count": 1}],
            "latest_observation": {"inventory": [{"name": "dirt", "count": 1}]},
        },
    )

    assert result["success"] is False
    assert result["checks"][0]["type"] == "inventory_delta_contains"
    assert result["checks"][0]["actual_delta"] == 0


@pytest.mark.anyio
async def test_block_placed_verifier_accepts_place_block_result() -> None:
    """Block placement verifier should accept a successful placement action result."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "block_placed", "block": "crafting_table"}},
        {
            "latest_action_result": {
                "ok": True,
                "action_type": "place_block",
                "item": "crafting_table",
                "target": {"x": 1, "y": 64, "z": 1},
            }
        },
    )

    assert result["success"] is True


@pytest.mark.anyio
async def test_entity_defeated_verifier_fails_when_entity_is_still_nearby() -> None:
    """Entity verifier should fail when the target entity remains in observation."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "entity_defeated", "entity": "zombie"}},
        {"latest_observation": {"nearby_entities": [{"name": "zombie", "type": "mob"}]}},
    )

    assert result["success"] is False
    assert "still present" in result["reason"]


@pytest.mark.anyio
async def test_entity_defeated_verifier_accepts_bounded_combat_result() -> None:
    """Legacy entity_defeated checks should accept the new engage_combat success shape."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "entity_defeated", "entity": "zombie"}},
        {
            "latest_action_result": {
                "ok": True,
                "action_type": "engage_combat",
                "entity": "zombie",
                "status": "target_killed",
            },
            "latest_observation": {"nearby_entities": [{"name": "zombie", "type": "mob"}]},
        },
    )

    assert result["success"] is True
    assert "Bounded combat killed zombie" in result["reason"]


@pytest.mark.anyio
async def test_composite_all_verifier_reports_nested_checks() -> None:
    """Composite verifiers should preserve nested check results for audit."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {
            "verifier": {
                "all": [
                    {"type": "inventory_contains", "item": "oak_planks", "count": 4},
                    {"type": "entity_defeated", "entity": "zombie"},
                ]
            }
        },
        {
            "latest_observation": {
                "inventory": [{"name": "oak_planks", "count": 4}],
                "nearby_entities": [],
            }
        },
    )

    assert result["success"] is True
    assert len(result["checks"]) == 2


@pytest.mark.anyio
async def test_entity_kill_delta_verifier_reads_stats() -> None:
    """Combat verifier should use kill-stat deltas instead of nearby-entity absence."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "entity_kill_delta", "entity": "zombie", "count": 1}},
        {
            "initial_observation": {"stats": {"kill_entity": {"zombie": 2}}},
            "latest_observation": {"stats": {"kill_entity": {"zombie": 3}}},
        },
    )

    assert result["success"] is True
    assert result["checks"][0]["actual_delta"] == 1


@pytest.mark.anyio
async def test_entity_kill_delta_verifier_accepts_confirmed_death_ledger_without_drops() -> None:
    """Combat verifier should accept entityDead evidence without requiring a dropped item."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "entity_kill_delta", "entity": "pig", "count": 1}},
        {
            "initial_observation": {
                "stats": {"confirmed_kill_entity": {}, "kill_count_source": "mineflayer_entity_dead"}
            },
            "latest_observation": {
                "stats": {
                    "confirmed_kill_entity": {"pig": 1},
                    "confirmed_kill_events": [
                        {
                            "sequence": 1,
                            "entity_id": 42,
                            "entity": "pig",
                            "attribution": "direct_damage",
                        }
                    ],
                    "kill_count_source": "mineflayer_entity_dead",
                },
                "nearby_entities": [],
            },
        },
    )

    assert result["success"] is True
    assert result["checks"][0]["actual_delta"] == 1
    assert result["checks"][0]["kill_count_source"] == "mineflayer_entity_dead"


@pytest.mark.anyio
async def test_item_used_delta_verifier_reads_stats() -> None:
    """TechTree verifier should use item-use stat deltas."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "item_used_delta", "item": "crafting_table", "count": 1}},
        {
            "initial_observation": {"stats": {"use_item": {"crafting_table": 0}}},
            "latest_observation": {"stats": {"use_item": {"crafting_table": 1}}},
        },
    )

    assert result["success"] is True
    assert result["checks"][0]["type"] == "item_used_delta"


@pytest.mark.anyio
async def test_time_alive_verifier_reads_world_ticks() -> None:
    """Survival verifier should evaluate alive-time deltas."""

    verifier = ProgrammaticVerifier()

    result = await verifier.verify(
        {"verifier": {"type": "time_alive", "ticks": 20}},
        {
            "initial_observation": {"world": {"age_ticks": 100}},
            "latest_observation": {"world": {"age_ticks": 125}},
        },
    )

    assert result["success"] is True
    assert result["checks"][0]["actual_delta"] == 25
