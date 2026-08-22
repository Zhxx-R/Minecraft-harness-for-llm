import json
from pathlib import Path

import pytest

from mc_agent_harness.tasks.minedojo_adapter import (
    MineDojoSpecMatcher,
    adapt_programmatic_catalog,
    adapt_programmatic_task,
    write_executable_manifest_jsonl,
)
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


def test_minedojo_adapter_uses_official_template_reset_metadata() -> None:
    """Official MineDojo template specs should drive verifier and reset metadata."""

    task = {
        "task_id": "combat_zombie_plains_iron_armors_diamond_sword_shield",
        "category": "combat",
        "family": "Combat",
        "goal": "combat a zombie in plains with gear",
        "knowledge_tags": ["minecraft:term/zombie"],
    }
    matcher = MineDojoSpecMatcher(
        {
            "combat_zombie_{combat_biomes}_{armor_material}_armors_{weapon_material}_sword_shield": {
                "__cls__": "combat",
                "target_names": "zombie",
                "target_quantities": 1,
                "initial_mobs": "zombie",
                "initial_mob_spawn_range_low": [-7, 1, -7],
                "initial_mob_spawn_range_high": [7, 1, 7],
                "initial_inventory": {
                    "mainhand": {"name": "{weapon_material}_sword"},
                    "head": {"name": "{armor_material}_helmet"},
                    "offhand": {"name": "shield"},
                },
                "specified_biome": "{combat_biomes}",
            }
        }
    )

    adaptation = adapt_programmatic_task(task, matcher=matcher)
    manifest = adaptation.manifest

    assert adaptation.supported is True
    assert manifest["verifier"] == {"type": "entity_kill_delta", "entity": "zombie", "count": 1}
    assert manifest["reset_plan"]["spawn_mobs"][0]["entity"] == "zombie"
    assert {"slot": "mainhand", "item": "diamond_sword", "count": 1} in manifest["reset_plan"]["initial_inventory"]
    assert {"slot": "head", "item": "iron_helmet", "count": 1} in manifest["reset_plan"]["initial_inventory"]
    assert manifest["reset_plan"]["biome_hint"] == "plains"
    assert manifest["minedojo"]["source_spec"]["confidence"] == "official_template"


def test_minedojo_adapter_expands_official_specs_without_ambiguous_regex_splits() -> None:
    """MineDojo enum expansion should keep extreme_hills separate from diamond armor."""

    task = {
        "task_id": "combat_skeleton_extreme_hills_diamond_armors_iron_sword_shield",
        "category": "combat",
        "family": "Combat",
        "goal": "combat a skeleton in night extreme hills with diamond armor",
        "knowledge_tags": ["minecraft:term/skeleton"],
    }
    matcher = MineDojoSpecMatcher(
        {
            "combat_{regular_biomes_mob}_{combat_biomes}_{armor_material}_armors_{weapon_material}_sword_shield": {
                "__cls__": "combat",
                "target_names": "{regular_biomes_mob}",
                "target_quantities": 1,
                "initial_mobs": "{regular_biomes_mob}",
                "initial_inventory": {
                    "mainhand": {"name": "{weapon_material}_sword"},
                    "feet": {"name": "{armor_material}_boots"},
                    "legs": {"name": "{armor_material}_leggings"},
                    "chest": {"name": "{armor_material}_chestplate"},
                    "head": {"name": "{armor_material}_helmet"},
                    "offhand": {"name": "shield"},
                },
                "specified_biome": "{combat_biomes}",
            },
            "combat_{regular_biomes_night_mob}_{combat_biomes}_{armor_material}_armors_{weapon_material}_sword_shield": {
                "__cls__": "combat",
                "target_names": "{regular_biomes_night_mob}",
                "target_quantities": 1,
                "initial_mobs": "{regular_biomes_night_mob}",
                "start_at_night": True,
                "initial_inventory": {
                    "mainhand": {"name": "{weapon_material}_sword"},
                    "feet": {"name": "{armor_material}_boots"},
                    "legs": {"name": "{armor_material}_leggings"},
                    "chest": {"name": "{armor_material}_chestplate"},
                    "head": {"name": "{armor_material}_helmet"},
                    "offhand": {"name": "shield"},
                },
                "specified_biome": "{combat_biomes}",
            },
        }
    )

    adaptation = adapt_programmatic_task(task, matcher=matcher)
    manifest = adaptation.manifest
    inventory = manifest["reset_plan"]["initial_inventory"]

    assert adaptation.supported is True
    assert manifest["verifier"] == {"type": "entity_kill_delta", "entity": "skeleton", "count": 1}
    assert manifest["reset_plan"]["biome_hint"] == "windswept_hills"
    assert manifest["reset_plan"]["set_time"] == "night"
    assert {"slot": "mainhand", "item": "iron_sword", "count": 1} in inventory
    assert {"slot": "feet", "item": "diamond_boots", "count": 1} in inventory
    assert {"slot": "legs", "item": "diamond_leggings", "count": 1} in inventory
    assert {"slot": "chest", "item": "diamond_chestplate", "count": 1} in inventory
    assert {"slot": "head", "item": "diamond_helmet", "count": 1} in inventory
    assert all("hills_diamond" not in item["item"] for item in inventory)
    assert manifest["minedojo"]["template_bindings"]["combat_biomes"] == "extreme_hills"
    assert manifest["minedojo"]["template_bindings"]["armor_material"] == "diamond"
    assert manifest["minedojo"]["template_bindings"]["regular_biomes_night_mob"] == "skeleton"


def test_minedojo_adapter_fallback_parses_harvest_count_and_biome() -> None:
    """Fallback parser should make catalog-only harvest tasks executable without target spawning."""

    adaptation = adapt_programmatic_task(
        {
            "task_id": "harvest_8_log_forest",
            "category": "harvest",
            "family": "Harvest",
            "goal": "obtain 8 log in forest",
            "knowledge_tags": [],
        }
    )

    assert adaptation.supported is True
    assert adaptation.manifest["verifier"]["item"] == "oak_log"
    assert adaptation.manifest["verifier"]["count"] == 8
    assert adaptation.manifest["verifier"]["require_delta"] is True
    assert adaptation.manifest["reset_plan"]["biome_hint"] == "forest"
    assert adaptation.manifest["reset_plan"]["set_blocks"] == []


def test_minedojo_adapter_maps_generic_wooden_button_to_oak_button() -> None:
    """MineDojo generic wooden_button targets should become executable oak_button goals."""

    adaptation = adapt_programmatic_task(
        {
            "task_id": "harvest_1_wooden_button",
            "category": "harvest",
            "family": "Harvest",
            "goal": "obtain 1 wooden_button",
            "knowledge_tags": [],
        }
    )

    assert adaptation.supported is True
    assert adaptation.manifest["verifier"]["item"] == "oak_button"
    assert adaptation.manifest["verifier"]["count"] == 1


def test_minedojo_adapter_prefers_specific_official_harvest_template() -> None:
    """Specific MineDojo harvest templates should beat broad catch-all templates."""

    task = {
        "task_id": "harvest_8_stone_forest_with_furnace_and_fuel",
        "category": "harvest",
        "family": "Harvest",
        "goal": "obtain 8 stone in forest with furnace and fuel",
        "knowledge_tags": [],
    }
    matcher = MineDojoSpecMatcher(
        {
            "harvest_{quantity}_{furnace_items}": {
                "__cls__": "harvest",
                "target_names": "{furnace_items}",
                "target_quantities": "{quantity}",
            },
            "harvest_{quantity}_{furnace_core}_{biome_subset}_with_furnace_and_fuel": {
                "__cls__": "harvest",
                "target_names": "{furnace_core}",
                "target_quantities": "{quantity}",
                "specified_biome": "{biome_subset}",
                "initial_inventory": {
                    "mainhand": {"name": "furnace"},
                    1: {"name": "coal", "quantity": 50},
                },
            },
        }
    )

    adaptation = adapt_programmatic_task(task, matcher=matcher)

    assert adaptation.manifest["verifier"]["item"] == "stone"
    assert adaptation.manifest["verifier"]["count"] == 8
    assert adaptation.manifest["reset_plan"]["biome_hint"] == "forest"
    assert {"slot": "hotbar.1", "item": "coal", "count": 50} in adaptation.manifest["reset_plan"]["initial_inventory"]


@pytest.mark.anyio
async def test_executable_manifest_jsonl_can_be_loaded_by_task_provider(tmp_path: Path) -> None:
    """Generated executable JSONL snapshots should be directly consumable by TaskProvider."""

    adaptations, summary = adapt_programmatic_catalog(
        [
            {
                "task_id": "harvest_1_log",
                "category": "harvest",
                "family": "Harvest",
                "goal": "obtain log",
                "knowledge_tags": [],
            }
        ]
    )
    output = tmp_path / "minedojo_executable.jsonl"
    write_executable_manifest_jsonl(adaptations, output_path=output, summary=summary, summary_path=tmp_path / "summary.json")

    loaded = await MineDojoTaskProvider(output).load_task("harvest_1_log")

    assert output.exists()
    assert json.loads((tmp_path / "summary.json").read_text())["supported_count"] == 1
    assert loaded["task_id"] == "harvest_1_log"
    assert loaded["minedojo"]["executable"] is True
