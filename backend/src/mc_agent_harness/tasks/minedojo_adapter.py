from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from mc_agent_harness.harness.tool_registry import DEFAULT_HARNESS_ACTIONS


EXECUTABLE_MANIFEST_SCHEMA = "mc-agent-harness.minedojo-executable-manifest.v1"

BIOME_ALIASES = {
    "extreme_hills": "windswept_hills",
    "mushroom_island": "mushroom_fields",
    "swampland": "swamp",
    "nether": "nether_wastes",
    "end": "the_end",
}

ENTITY_ALIASES = {
    "zombie_pigman": "zombified_piglin",
}

ITEM_ALIASES = {
    "log": "oak_log",
    "milk": "milk_bucket",
    "planks": "oak_planks",
    "wooden_button": "oak_button",
    "wool": "white_wool",
    "wood": "oak_log",
}

BIOME_TOKENS = {
    "desert",
    "end",
    "extreme_hills",
    "forest",
    "jungle",
    "mushroom_island",
    "nether",
    "plains",
    "swampland",
    "taiga",
}

MINEDOJO_TEMPLATE_VALUES: dict[str, list[str | int]] = {
    "combat_biomes": ["forest", "plains", "extreme_hills"],
    "regular_biomes_mob": ["cow", "pig", "sheep", "chicken"],
    "regular_biomes_night_mob": ["zombie", "spider", "skeleton", "creeper", "witch", "enderman"],
    "end_mob": ["shulker", "endermite", "enderman"],
    "nether_mob": ["blaze", "ghast", "wither_skeleton", "zombie_pigman"],
    "plains_mob": ["horse", "donkey"],
    "weapon_material": ["wooden", "iron", "diamond"],
    "armor_material": ["leather", "iron", "diamond"],
    "quantity": [1, 8],
    "cow_biomes": ["plains", "extreme_hills", "forest"],
    "sheep_biomes": ["plains", "extreme_hills", "forest"],
    "ore_type": ["iron_ore", "gold_ore", "diamond", "redstone", "coal", "cobblestone"],
    "pickaxe_material": ["wooden", "stone", "iron", "golden", "diamond"],
    "natural_items": [
        "nether_star",
        "blaze_rod",
        "ghast_tear",
        "nether_wart",
        "netherrack",
        "soul_sand",
        "chorus_flower",
        "chorus_fruit",
        "chorus_plant",
        "elytra",
        "end_stone",
        "ender_pearl",
        "apple",
        "beef",
        "beetroot",
        "beetroot_seeds",
        "bone",
        "brown_mushroom",
        "cactus",
        "carrot",
        "chicken",
        "dirt",
        "egg",
        "feather",
        "fish",
        "grass",
        "leaves",
        "log",
        "monster_egg",
        "mutton",
        "porkchop",
        "potato",
        "prismarine_shard",
        "pumpkin",
        "rabbit",
        "red_mushroom",
        "reeds",
        "sapling",
        "skull",
        "snowball",
        "spawn_egg",
        "sponge",
        "string",
        "totem_of_undying",
        "vine",
        "web",
        "wheat_seeds",
        "wheat",
    ],
    "craft_items": [
        "book",
        "carrot_on_a_stick",
        "clay",
        "crafting_table",
        "dye",
        "end_bricks",
        "end_rod",
        "ender_eye",
        "flint_and_steel",
        "glowstone",
        "gold_nugget",
        "iron_nugget",
        "iron_trapdoor",
        "lever",
        "nether_brick",
        "planks",
        "pumpkin_seeds",
        "red_nether_brick",
        "sandstone",
        "shears",
        "slime_ball",
        "stick",
        "stone_button",
        "stonebrick",
        "sugar",
        "torch",
        "trapped_chest",
        "wooden_button",
        "wool",
        "stone_pressure_plate",
    ],
    "crafting_table_items": [
        "anvil",
        "arrow",
        "banner",
        "beacon",
        "bed",
        "beetroot_soup",
        "boat",
        "bookshelf",
        "bowl",
        "bread",
        "bucket",
        "cake",
        "cauldron",
        "chest",
        "cookie",
        "end_crystal",
        "ender_chest",
        "fence",
        "fence_gate",
        "fire_charge",
        "fishing_rod",
        "flower_pot",
        "furnace",
        "glass_bottle",
        "glass_pane",
        "golden_apple",
        "hopper",
        "iron_bars",
        "ladder",
        "lead",
        "map",
        "minecart",
        "mushroom_stew",
        "painting",
        "paper",
        "pumpkin_pie",
        "rabbit_stew",
        "rail",
        "sea_lantern",
        "shield",
        "sign",
        "speckled_melon",
        "stone_slab",
        "trapdoor",
        "tripwire_hook",
        "wooden_door",
        "writable_book",
    ],
    "furnace_items": [
        "baked_potato",
        "brick",
        "cooked_beef",
        "cooked_chicken",
        "cooked_fish",
        "cooked_mutton",
        "cooked_porkchop",
        "cooked_rabbit",
        "glass",
        "gold_ingot",
        "iron_ingot",
        "quartz",
        "stone",
        "emerald",
        "netherbrick",
    ],
    "biome_subset": ["plains", "jungle", "taiga", "forest", "swampland"],
    "natural_core": ["apple", "beef", "bone", "chicken", "log", "reeds", "web", "wheat"],
    "hand_craft_core": ["flint_and_steel", "crafting_table", "planks", "shears", "stick", "sugar", "torch"],
    "crafting_table_core": ["arrow", "chest", "shield", "fishing_rod", "bucket", "furnace"],
    "furnace_core": ["cooked_beef", "glass", "gold_ingot", "iron_ingot", "brick", "stone"],
    "from_barehand_tools": ["wooden", "stone"],
    "from_barehand_tools_armor": ["iron", "golden", "diamond"],
    "from_wood_tools": ["stone"],
    "from_wood_tools_armor": ["iron", "golden", "diamond"],
    "from_stone_tools_armor": ["iron", "golden", "diamond"],
    "from_iron_tools_armor": ["golden", "diamond"],
    "from_gold_tools_armor": ["diamond"],
    "target_tools": ["sword", "pickaxe", "axe", "hoe", "shovel"],
    "target_armor": ["boots", "chestplate", "helmet", "leggings"],
    "target_tools_armor": ["sword", "pickaxe", "axe", "hoe", "shovel", "boots", "chestplate", "helmet", "leggings"],
    "redstone_list": [
        "redstone_block",
        "clock",
        "compass",
        "dispenser",
        "dropper",
        "observer",
        "piston",
        "redstone_lamp",
        "redstone_torch",
        "repeater",
        "detector_rail",
        "comparator",
        "activator_rail",
    ],
}


@dataclass(frozen=True, slots=True)
class MineDojoTemplateMatch:
    """One matched MineDojo template spec plus resolved placeholder bindings."""

    template_id: str
    bindings: dict[str, str]
    spec: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MineDojoManifestAdaptation:
    """Result of converting one MineDojo catalog row into a harness executable manifest."""

    task_id: str
    supported: bool
    manifest: dict[str, Any]
    unsupported_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MineDojoManifestBuildSummary:
    """Summary emitted after adapting a MineDojo programmatic catalog batch."""

    schema_version: str
    task_count: int
    supported_count: int
    unsupported_count: int
    categories: dict[str, int]
    unsupported_reasons: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Convert the build summary into a JSON-safe payload."""

        return asdict(self)


class MineDojoSpecMatcher:
    """Template matcher for official MineDojo tasks_specs.yaml keys."""

    def __init__(self, task_specs: dict[str, Any] | None = None) -> None:
        self.task_specs = task_specs or {}
        self._expanded = _expand_official_task_specs(self.task_specs)

    def match(self, task_id: str) -> MineDojoTemplateMatch | None:
        """Return the MineDojo-style exact expanded task spec for one task id."""

        return self._expanded.get(task_id)


def adapt_programmatic_catalog(
    tasks: Iterable[dict[str, Any]],
    *,
    task_specs: dict[str, Any] | None = None,
) -> tuple[list[MineDojoManifestAdaptation], MineDojoManifestBuildSummary]:
    """Adapt a batch of local MineDojo catalog rows into executable harness manifests."""

    matcher = MineDojoSpecMatcher(task_specs)
    adaptations = [adapt_programmatic_task(task, matcher=matcher) for task in tasks]
    categories: dict[str, int] = {}
    unsupported: dict[str, int] = {}
    for adaptation in adaptations:
        category = str(adaptation.manifest.get("category") or "unknown")
        categories[category] = categories.get(category, 0) + 1
        if not adaptation.supported:
            reason = adaptation.unsupported_reason or "unknown"
            unsupported[reason] = unsupported.get(reason, 0) + 1
    summary = MineDojoManifestBuildSummary(
        schema_version=EXECUTABLE_MANIFEST_SCHEMA,
        task_count=len(adaptations),
        supported_count=sum(1 for item in adaptations if item.supported),
        unsupported_count=sum(1 for item in adaptations if not item.supported),
        categories=dict(sorted(categories.items())),
        unsupported_reasons=dict(sorted(unsupported.items())),
    )
    return adaptations, summary


def adapt_programmatic_task(
    task: dict[str, Any],
    *,
    matcher: MineDojoSpecMatcher | None = None,
) -> MineDojoManifestAdaptation:
    """Convert one catalog task into a harness task manifest with reset/verifier metadata."""

    task_id = str(task.get("task_id") or "")
    category = str(task.get("category") or _infer_category(task_id))
    match = matcher.match(task_id) if matcher is not None else None
    parsed = _parsed_task(task, match)
    if parsed.get("unsupported_reason"):
        manifest = _base_manifest(task, category)
        manifest["minedojo"]["executable"] = False
        manifest["minedojo"]["unsupported_reason"] = parsed["unsupported_reason"]
        return MineDojoManifestAdaptation(
            task_id=task_id,
            supported=False,
            manifest=manifest,
            unsupported_reason=str(parsed["unsupported_reason"]),
        )

    verifier = _verifier_for(category, parsed)
    reset_plan = _reset_plan_for(category, parsed)
    manifest = {
        **_base_manifest(task, category),
        "schema_version": EXECUTABLE_MANIFEST_SCHEMA,
        "allowed_actions": list(DEFAULT_HARNESS_ACTIONS),
        "verifier": verifier,
        "success_criteria": verifier,
        "reset_plan": reset_plan,
        "minedojo": {
            **_base_manifest(task, category)["minedojo"],
            "catalog_only": False,
            "executable": True,
            "adapter": "minedojo_programmatic_v1",
            "template_id": match.template_id if match else None,
            "template_bindings": match.bindings if match else {},
            "source_spec": {
                "confidence": "official_template" if match else "task_id_fallback",
                "official_template_available": bool(match),
                "raw_spec_keys": sorted(match.spec.keys()) if match else [],
            },
            "official_guidance_available": bool(task.get("guidance")),
            "guidance_policy": "metadata_only_not_auto_prompted",
        },
    }
    return MineDojoManifestAdaptation(task_id=task_id, supported=True, manifest=manifest)


def write_executable_manifest_jsonl(
    adaptations: Iterable[MineDojoManifestAdaptation],
    *,
    output_path: str | Path,
    summary: MineDojoManifestBuildSummary | None = None,
    summary_path: str | Path | None = None,
) -> Path:
    """Write supported executable manifests to a JSONL snapshot."""

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    supported = [item.manifest for item in adaptations if item.supported]
    output.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in supported)
        + ("\n" if supported else ""),
        encoding="utf-8",
    )
    if summary is not None and summary_path is not None:
        summary_output = Path(summary_path)
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(
            json.dumps(summary.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return output


def _base_manifest(task: dict[str, Any], category: str) -> dict[str, Any]:
    """Build common manifest fields before category-specific adaptation."""

    task_id = str(task.get("task_id") or "")
    minedojo = task.get("minedojo") if isinstance(task.get("minedojo"), dict) else {}
    return {
        "task_id": task_id,
        "source": "minedojo",
        "category": category,
        "family": task.get("family") or category.title(),
        "goal": str(task.get("goal") or task.get("prompt") or task_id.replace("_", " ")),
        "description": f"Executable MineDojo programmatic task adapted from {task_id}.",
        "allowed_actions": list(DEFAULT_HARNESS_ACTIONS),
        "verifier": {"type": "inventory_contains", "item": "__unsupported__", "count": 1},
        "success_criteria": {"type": "inventory_contains", "item": "__unsupported__", "count": 1},
        "knowledge_tags": list(task.get("knowledge_tags", [])),
        "runtime_profile": "live-mineflayer-training",
        "benchmark_suite_tier": task.get("benchmark_suite_tier"),
        "minedojo": {
            **minedojo,
            "programmatic": True,
            "creative": False,
        },
    }


def _parsed_task(task: dict[str, Any], match: MineDojoTemplateMatch | None) -> dict[str, Any]:
    """Build normalized category metadata from official specs or task id fallback parsing."""

    category = str(task.get("category") or _infer_category(str(task.get("task_id") or "")))
    if match is not None:
        return _parsed_from_official_spec(task, match)
    if category == "harvest":
        return _parse_harvest_task_id(str(task.get("task_id") or ""))
    if category == "combat":
        return _parse_combat_task_id(str(task.get("task_id") or ""))
    if category == "techtree":
        return _parse_techtree_task_id(str(task.get("task_id") or ""))
    if category == "survival":
        return _parse_survival_task_id(str(task.get("task_id") or ""))
    return {"unsupported_reason": f"unsupported_category:{category}"}


def _parsed_from_official_spec(task: dict[str, Any], match: MineDojoTemplateMatch) -> dict[str, Any]:
    """Normalize a matched official MineDojo template spec into adapter fields."""

    spec = match.spec
    category = str(spec.get("__cls__") or task.get("category") or "")
    target_names = _as_list(spec.get("target_names") or spec.get("tech_item"))
    target_quantities = _as_list(spec.get("target_quantities") or 1)
    targets = [
        {
            "name": _canonical_item_or_entity(str(name), category),
            "quantity": int(target_quantities[index] if index < len(target_quantities) else target_quantities[-1]),
        }
        for index, name in enumerate(target_names)
        if str(name)
    ]
    parsed: dict[str, Any] = {
        "category": category,
        "targets": targets,
        "target_days": int(spec.get("target_days", 1) or 1),
        "initial_inventory": _normalize_initial_inventory(spec.get("initial_inventory")),
        "specified_biome": _canonical_biome(spec.get("specified_biome")),
        "initial_mobs": [
            {
                "entity": _canonical_entity(str(name)),
                "count": 1,
                "range_low": spec.get("initial_mob_spawn_range_low", [-7, 1, -7]),
                "range_high": spec.get("initial_mob_spawn_range_high", [7, 1, 7]),
            }
            for name in _as_list(spec.get("initial_mobs"))
        ],
        "start_at_night": bool(spec.get("start_at_night") or spec.get("always_night")),
        "allow_mob_spawn": spec.get("allow_mob_spawn"),
        "tech_item": _canonical_item(str(spec.get("tech_item"))) if spec.get("tech_item") else None,
        "source": "official_template",
    }
    if category == "techtree" and not parsed["targets"] and parsed["tech_item"]:
        parsed["targets"] = [{"name": parsed["tech_item"], "quantity": 1}]
    if not parsed["targets"] and category != "survival":
        return {"unsupported_reason": "missing_targets_from_official_spec"}
    return parsed


def _parse_harvest_task_id(task_id: str) -> dict[str, Any]:
    """Parse a MineDojo harvest task id when official template specs are unavailable."""

    body = task_id.removeprefix("harvest_")
    count = 1
    tokens = body.split("_")
    if tokens and tokens[0].isdigit():
        count = int(tokens[0])
        body = "_".join(tokens[1:])
    initial_inventory: list[dict[str, Any]] = []
    initial_mobs: list[dict[str, Any]] = []
    body, biome = _strip_biome_suffix(body)
    body = _strip_suffix(body, "_with_crafting_table", initial_inventory, "crafting_table")
    body = _strip_suffix(body, "_with_empty_bucket", initial_inventory, "bucket")
    body = _strip_suffix(body, "_with_an_empty_bucket", initial_inventory, "bucket")
    body = _strip_suffix(body, "_with_shears", initial_inventory, "shears")
    if body.endswith("_with_cow"):
        body = body[: -len("_with_cow")]
        initial_mobs.append({"entity": "cow", "count": 1, "range_low": [-4, 0, -4], "range_high": [4, 0, 4]})
    if body.endswith("_with_sheep"):
        body = body[: -len("_with_sheep")]
        initial_mobs.append({"entity": "sheep", "count": 1, "range_low": [-4, 0, -4], "range_high": [4, 0, 4]})
    if body.endswith("_and_cow"):
        body = body[: -len("_and_cow")]
        initial_mobs.append({"entity": "cow", "count": 1, "range_low": [-4, 0, -4], "range_high": [4, 0, 4]})
    if body.endswith("_and_sheep"):
        body = body[: -len("_and_sheep")]
        initial_mobs.append({"entity": "sheep", "count": 1, "range_low": [-4, 0, -4], "range_high": [4, 0, 4]})
    target = _canonical_item(body)
    if not target:
        return {"unsupported_reason": "unparseable_harvest_target"}
    return {
        "category": "harvest",
        "targets": [{"name": target, "quantity": count}],
        "initial_inventory": initial_inventory,
        "specified_biome": biome,
        "initial_mobs": initial_mobs,
        "source": "task_id_fallback",
    }


def _parse_combat_task_id(task_id: str) -> dict[str, Any]:
    """Parse a MineDojo combat task id when official template specs are unavailable."""

    body = task_id.removeprefix("combat_")
    gear_start = _gear_start_index(body.split("_"))
    target_and_biome = "_".join(body.split("_")[:gear_start])
    gear_tokens = body.split("_")[gear_start:]
    target_and_biome, biome = _strip_biome_suffix(target_and_biome)
    entity = _canonical_entity(target_and_biome)
    initial_inventory = _combat_initial_inventory(gear_tokens)
    return {
        "category": "combat",
        "targets": [{"name": entity, "quantity": 1}],
        "initial_inventory": initial_inventory,
        "specified_biome": biome,
        "initial_mobs": [{"entity": entity, "count": 1, "range_low": [-7, 1, -7], "range_high": [7, 1, 7]}],
        "start_at_night": "night" in task_id or entity in {"zombie", "skeleton", "husk", "slime", "bat"},
        "source": "task_id_fallback",
    }


def _parse_techtree_task_id(task_id: str) -> dict[str, Any]:
    """Parse a MineDojo techtree task id when official template specs are unavailable."""

    match = re.fullmatch(r"techtree_from_(?P<start>[a-z0-9_]+)_to_(?P<target>[a-z0-9_]+)", task_id)
    if match is None:
        return {"unsupported_reason": "unparseable_techtree_target"}
    start = match.group("start")
    target = _canonical_item(match.group("target"))
    return {
        "category": "techtree",
        "targets": [{"name": target, "quantity": 1}],
        "initial_inventory": _techtree_initial_inventory(start),
        "specified_biome": None,
        "initial_mobs": [],
        "tech_item": target,
        "source": "task_id_fallback",
    }


def _parse_survival_task_id(task_id: str) -> dict[str, Any]:
    """Parse a MineDojo survival task id when official template specs are unavailable."""

    initial_inventory = []
    if "sword_food" in task_id:
        initial_inventory = [
            {"slot": "mainhand", "item": "wooden_sword", "count": 1},
            {"slot": "hotbar.1", "item": "cooked_beef", "count": 8},
        ]
    return {
        "category": "survival",
        "targets": [],
        "target_days": 1,
        "initial_inventory": initial_inventory,
        "specified_biome": None,
        "initial_mobs": [],
        "source": "task_id_fallback",
    }


def _verifier_for(category: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """Build a harness verifier from normalized MineDojo task metadata."""

    targets = parsed.get("targets") if isinstance(parsed.get("targets"), list) else []
    if category == "harvest":
        return _inventory_verifier(targets)
    if category == "combat":
        return _entity_kill_verifier(targets)
    if category == "techtree":
        target = (targets[0] if targets else {}).get("name") or parsed.get("tech_item")
        return {"type": "item_used_delta", "item": target, "count": 1}
    if category == "survival":
        return {"type": "time_alive", "target_days": int(parsed.get("target_days") or 1)}
    return {"type": "inventory_contains", "item": "__unsupported__", "count": 1}


def _reset_plan_for(category: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """Build a MineDojo-style server reset plan for one executable manifest."""

    _ = category
    return {
        "type": "minedojo_fast_reset",
        "clear_inventory": True,
        "clear_dropped_items": True,
        "set_time": "night" if parsed.get("start_at_night") else None,
        "set_weather": "clear",
        "biome_hint": parsed.get("specified_biome"),
        "random_teleport": {"enabled": False, "spread_distance": 0, "max_range": 200},
        "initial_inventory": list(parsed.get("initial_inventory") or []),
        "spawn_mobs": list(parsed.get("initial_mobs") or []),
        "set_blocks": [],
        "notes": [
            "Aligned with MineDojo fast reset semantics where possible through vanilla server commands.",
            "biome_hint is advisory unless the server pool provides biome-specific worlds or teleport tables.",
        ],
    }


def _inventory_verifier(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an inventory delta verifier for one or more target items."""

    checks = [
        {
            "type": "inventory_contains",
            "item": str(target.get("name")),
            "count": int(target.get("quantity", 1)),
            "require_delta": True,
        }
        for target in targets
    ]
    return checks[0] if len(checks) == 1 else {"all": checks}


def _entity_kill_verifier(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an entity-kill stat verifier for one or more target mobs."""

    checks = [
        {
            "type": "entity_kill_delta",
            "entity": str(target.get("name")),
            "count": int(target.get("quantity", 1)),
        }
        for target in targets
    ]
    return checks[0] if len(checks) == 1 else {"all": checks}


def _expand_official_task_specs(task_specs: dict[str, Any]) -> dict[str, MineDojoTemplateMatch]:
    """Expand MineDojo tasks_specs templates into exact task-id keyed specs."""

    expanded: dict[str, MineDojoTemplateMatch] = {}
    for template_id, spec in task_specs.items():
        if not isinstance(template_id, str) or not isinstance(spec, dict):
            continue
        placeholders = sorted(_template_placeholders(template_id, spec))
        if not placeholders:
            expanded[template_id] = MineDojoTemplateMatch(template_id=template_id, bindings={}, spec=dict(spec))
            continue
        if any(name not in MINEDOJO_TEMPLATE_VALUES for name in placeholders):
            continue
        names = tuple(placeholders)
        value_lists = [MINEDOJO_TEMPLATE_VALUES[name] for name in names]
        for values in product(*value_lists):
            bindings = {name: str(value) for name, value in zip(names, values, strict=True)}
            filled_task_id = template_id.format(**bindings)
            filled_spec = _format_template_value(spec, bindings)
            if isinstance(filled_spec.get("target_quantities"), str):
                filled_spec["target_quantities"] = int(filled_spec["target_quantities"])
            if isinstance(filled_spec.get("prompt"), str):
                filled_spec["prompt"] = filled_spec["prompt"].replace("_", " ").replace(" 1", "")
            expanded[filled_task_id] = MineDojoTemplateMatch(
                template_id=template_id,
                bindings=dict(bindings),
                spec=filled_spec,
            )
    return expanded


def _template_placeholders(template_id: str, spec: dict[str, Any]) -> set[str]:
    """Collect template placeholders from task id and nested spec values."""

    placeholders = set(re.findall(r"\{(.*?)\}", template_id))

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, list):
            for nested in value:
                visit(nested)
            return
        if isinstance(value, str):
            placeholders.update(re.findall(r"\{(.*?)\}", value))

    visit(spec)
    return placeholders


def _format_template_value(value: Any, bindings: dict[str, str]) -> Any:
    """Recursively substitute matched template bindings into a spec value."""

    if isinstance(value, str):
        result = value
        for key, replacement in bindings.items():
            result = result.replace(f"{{{key}}}", replacement)
        return result
    if isinstance(value, list):
        return [_format_template_value(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: _format_template_value(item, bindings) for key, item in value.items()}
    return value


def _normalize_initial_inventory(value: Any) -> list[dict[str, Any]]:
    """Convert MineDojo initial_inventory slot mappings into command-ready entries."""

    if not isinstance(value, dict):
        return []
    entries: list[dict[str, Any]] = []
    for slot, item_spec in value.items():
        if not isinstance(item_spec, dict):
            continue
        item_name = item_spec.get("name")
        if not isinstance(item_name, str) or not item_name:
            continue
        entries.append(
            {
                "slot": _canonical_slot(str(slot)),
                "item": _canonical_item(item_name),
                "count": int(item_spec.get("quantity") or item_spec.get("count") or 1),
            }
        )
    return entries


def _combat_initial_inventory(tokens: list[str]) -> list[dict[str, Any]]:
    """Infer combat starting equipment from a fallback-parsed task id suffix."""

    if not tokens or tokens == ["barehand"]:
        return []
    text = "_".join(tokens)
    inventory: list[dict[str, Any]] = []
    weapon_match = re.search(r"(wooden|iron|diamond|stone|golden)_sword", text)
    armor_match = re.search(r"(leather|iron|diamond|golden)_armors", text)
    if weapon_match:
        inventory.append({"slot": "mainhand", "item": f"{weapon_match.group(1)}_sword", "count": 1})
    if "shield" in tokens:
        inventory.append({"slot": "offhand", "item": "shield", "count": 1})
    if armor_match:
        material = armor_match.group(1)
        inventory.extend(
            [
                {"slot": "head", "item": f"{material}_helmet", "count": 1},
                {"slot": "chest", "item": f"{material}_chestplate", "count": 1},
                {"slot": "legs", "item": f"{material}_leggings", "count": 1},
                {"slot": "feet", "item": f"{material}_boots", "count": 1},
            ]
        )
    return inventory


def _techtree_initial_inventory(start: str) -> list[dict[str, Any]]:
    """Infer a minimal MineDojo-style starting inventory for fallback techtree tasks."""

    if start == "barehand":
        return []
    if start == "wood":
        return [
            {"slot": "hotbar.0", "item": "oak_log", "count": 16},
            {"slot": "hotbar.1", "item": "crafting_table", "count": 1},
        ]
    if start == "stone":
        return [
            {"slot": "hotbar.0", "item": "cobblestone", "count": 32},
            {"slot": "hotbar.1", "item": "oak_log", "count": 16},
            {"slot": "hotbar.2", "item": "crafting_table", "count": 1},
        ]
    return []


def _as_list(value: Any) -> list[Any]:
    """Normalize scalars and lists from MineDojo specs into a list."""

    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _strip_biome_suffix(text: str) -> tuple[str, str | None]:
    """Remove a known MineDojo biome suffix from a task-id segment."""

    for biome in sorted(BIOME_TOKENS, key=len, reverse=True):
        suffix = f"_{biome}"
        if text == biome:
            return "", _canonical_biome(biome)
        if text.endswith(suffix):
            return text[: -len(suffix)], _canonical_biome(biome)
    return text, None


def _strip_suffix(text: str, suffix: str, inventory: list[dict[str, Any]], item: str) -> str:
    """Remove an initial-inventory suffix and append the corresponding item."""

    if not text.endswith(suffix):
        return text
    inventory.append({"slot": f"hotbar.{len(inventory)}", "item": item, "count": 1})
    return text[: -len(suffix)]


def _gear_start_index(tokens: list[str]) -> int:
    """Find the first suffix token that describes combat equipment rather than target/biome."""

    for index, token in enumerate(tokens):
        if token in {"barehand", "diamond", "iron", "wooden", "leather", "golden", "stone"}:
            return index
    return len(tokens)


def _canonical_slot(slot: str) -> str:
    """Normalize MineDojo equipment slot names into command-plan slot names."""

    if slot.isdigit():
        return f"hotbar.{slot}"
    return {
        "mainhand": "mainhand",
        "offhand": "offhand",
        "head": "head",
        "chest": "chest",
        "legs": "legs",
        "feet": "feet",
    }.get(slot, slot)


def _canonical_item_or_entity(name: str, category: str) -> str:
    """Canonicalize a target name according to the task category."""

    return _canonical_entity(name) if category == "combat" else _canonical_item(name)


def _canonical_item(name: str) -> str:
    """Map MineDojo legacy item names to Mineflayer/Minecraft 1.20-style ids when known."""

    if not name:
        return name
    return ITEM_ALIASES.get(name, name)


def _canonical_entity(name: str) -> str:
    """Map MineDojo legacy entity names to Minecraft 1.20-style ids when known."""

    return ENTITY_ALIASES.get(name, name)


def _canonical_biome(value: Any) -> str | None:
    """Map MineDojo legacy biome names to Minecraft 1.20-style ids when known."""

    if not isinstance(value, str) or not value:
        return None
    return BIOME_ALIASES.get(value, value)


def _canonicalize_binding(key: str, value: str) -> str:
    """Canonicalize template bindings only where placeholders carry legacy naming."""

    if key.endswith("mob"):
        return _canonical_entity(value)
    return value


def _infer_category(task_id: str) -> str:
    """Infer a MineDojo category from its programmatic task id."""

    if task_id.startswith("combat_"):
        return "combat"
    if task_id.startswith("harvest_"):
        return "harvest"
    if task_id.startswith("techtree_"):
        return "techtree"
    if task_id.startswith("survival"):
        return "survival"
    return "programmatic"
