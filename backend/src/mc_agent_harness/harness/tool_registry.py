from collections.abc import Iterable

from mc_agent_harness.schemas.action import ActionType, HarnessAction


CANONICAL_KNOWLEDGE_ACTIONS: tuple[ActionType, ...] = (
    "resolve_terms",
    "get_recipe",
    "retrieve_docs",
)

CANONICAL_PRIMITIVE_ACTIONS: tuple[ActionType, ...] = (
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
)

CANONICAL_CONTROL_ACTIONS: tuple[ActionType, ...] = ("submit_for_evaluation",)

DEFAULT_HARNESS_ACTIONS: tuple[ActionType, ...] = (
    CANONICAL_KNOWLEDGE_ACTIONS + CANONICAL_PRIMITIVE_ACTIONS + CANONICAL_CONTROL_ACTIONS
)
DEFAULT_WEEK3_ACTIONS: tuple[ActionType, ...] = CANONICAL_PRIMITIVE_ACTIONS
DEFAULT_WEEK5_ACTIONS: tuple[ActionType, ...] = CANONICAL_PRIMITIVE_ACTIONS

PROMPT_HIDDEN_ACTIONS: frozenset[ActionType] = frozenset(
    {"execute_skill", "fight_entity", "engage_combat", "craft_item", "smelt_item"}
)

PRIMITIVE_HARVEST_ACTIONS: tuple[ActionType, ...] = (
    "scan_blocks",
    "scan_dropped_items",
    "move_to",
    "dig_block_at",
    "place_block",
    "wait_ticks",
    "query_inventory",
    "request_visual_snapshot",
)

ACTION_PRIMITIVE_GUIDE: dict[ActionType, dict[str, object]] = {
    "resolve_terms": {
        "purpose": "Resolve Minecraft terms, aliases, item ids, block ids, and entity names from task text or observations.",
        "args": {
            "text": "text to resolve, for example a task goal or phrase containing Minecraft terms",
            "limit": "optional maximum number of terms to return",
        },
        "returns": "canonical ids, kinds, aliases, descriptions, tags, and recipe hints when available.",
        "when_to_use": "Use when a task mentions an unclear Minecraft-specific term, alias, block, item, or entity.",
    },
    "get_recipe": {
        "purpose": (
            "Look up a deterministic local item-processing recipe by canonical item id, "
            "including inventory crafting, crafting-table crafting, and furnace smelting/cooking."
        ),
        "args": {
            "item": "canonical output item id, for example oak_planks, wooden_pickaxe, glass, iron_ingot, or cooked_beef"
        },
        "returns": "output count, input ingredients, required station, prerequisites such as fuel, and recipe description.",
        "when_to_use": (
            "Use before process_item when the input item, output count, station, fuel requirement, "
            "or whether the item is crafted vs smelted is uncertain."
        ),
    },
    "retrieve_docs": {
        "purpose": "Retrieve read-only local Minecraft or Mineflayer documentation snippets.",
        "args": {
            "query": "short search query",
            "limit": "optional maximum number of snippets",
            "scope": "optional source scope such as local, minecraft, mineflayer, or wiki",
        },
        "returns": "audited documentation snippets with source ids, titles, tags, and truncated content.",
        "when_to_use": "Use when term resolution or recipes are not enough to decide the next action.",
    },
    "scan_blocks": {
        "purpose": "Scan nearby loaded blocks by canonical block id without changing the world.",
        "args": {
            "block": "optional canonical block id, for example oak_log or dirt",
            "max_distance": "optional scan radius in blocks",
            "count": "optional maximum number of returned blocks",
        },
        "returns": "blocks with name, position, distance, and can_dig.",
        "when_to_use": "Use when the exact coordinate of a relevant block is unknown.",
    },
    "scan_entities": {
        "purpose": (
            "Scan the currently loaded nearby entities and interaction/combat affordances without "
            "moving, attacking, or exploring new terrain. Repeating the same broad scan from the "
            "same position usually observes the same loaded area."
        ),
        "args": {
            "entity": "optional canonical entity name, for example zombie, sheep, bat, ghast, or skeleton",
            "entity_id": "optional exact numeric entity id when re-scanning one previously observed entity",
            "max_distance": "optional scan radius in blocks",
            "count": "optional maximum number of returned entities",
        },
        "returns": (
            "entities with canonical numeric entities[].entity_id, legacy entities[].id alias, "
            "position, distance, airborne estimate, line of sight, melee reachability, and "
            "suggested combat modes. Each entity also includes bounded details sourced from the "
            "connected server packets and the versioned Minecraft registry: exact entity type id, "
            "registry type/category, dimensions, equipment/effects, registry-named raw metadata, "
            "and version-gated Minestom-derived semantic decoding for recognized packed fields "
            "(for example shared fire/invisibility flags and sheep wool color/sheared state). "
            "Raw scan results retain bounded server details; prompt context keeps entity-specific "
            "metadata, semantic decoding, and a small useful common subset. Unknown fields stay raw "
            "instead of being guessed."
        ),
        "when_to_use": (
            "Use when an entity target's identity, exact entity_id, position, semantic metadata, "
            "reachability, or interaction/combat affordance is uncertain. If no returned entity "
            "satisfies the task criteria—including when task memory rules out every candidate—do "
            "not repeat an unchanged broad scan from the same position or only increase count/"
            "max_distance. Use move_to to relocate a meaningful distance (normally tens of blocks, "
            "for example 32-64 when terrain permits) toward a different reachable area, then scan "
            "again. Re-scan from the same position only when there is evidence that the world or "
            "target changed, an active follow changed relative position, or an exact entity_id "
            "needs a fresh detail snapshot."
        ),
    },
    "scan_dropped_items": {
        "purpose": "Scan nearby dropped item entities without moving or changing the world.",
        "args": {
            "item": "optional canonical item id, for example oak_log or dirt",
            "max_distance": "optional scan radius in blocks",
            "count": "optional maximum number of returned drops",
        },
        "returns": "dropped item entities with item, count, position, and distance.",
        "when_to_use": "Use after digging or when a relevant drop may be nearby but its coordinate is uncertain.",
    },
    "move_to": {
        "purpose": (
            "Navigate the bot toward a nearby coordinate using Mineflayer pathfinder. "
            "The worker may walk, jump, sprint, dig reachable blocking blocks, and use safe scaffold blocks automatically."
        ),
        "args": {
            "position": {"x": "number", "y": "number", "z": "number"},
            "tolerance": "optional acceptable distance from target",
            "timeout_ms": "optional movement timeout",
        },
        "returns": (
            "final distance from target, path status, break/place diagnostics, safe scaffold availability, "
            "nearest_reachable_position on partial paths, and movement diagnostics on failure."
        ),
        "when_to_use": "Use after selecting a concrete block, item, entity, or navigation coordinate from evidence.",
    },
    "follow": {
        "purpose": (
            "Start dynamically following one loaded moving entity and return immediately. "
            "The worker keeps following during observation and model reasoning, then automatically "
            "stops immediately before the next action executes."
        ),
        "args": {
            "entity_id": (
                "preferred numeric entities[].entity_id returned by scan_entities; required unless "
                "entity is provided, and keeps follow locked to the selected entity"
            ),
            "entity": (
                "fallback canonical entity name when no entity_id is available; follows the nearest "
                "matching loaded entity"
            ),
            "follow_distance": (
                "optional desired distance from the entity, minimum 0.5 and default 1.25; "
                "use about 1.25 for a subsequent use_item"
            ),
            "max_distance": (
                "optional maximum acquisition distance for the initially selected entity; "
                "defaults to 128 blocks"
            ),
        },
        "returns": (
            "the selected target, active persistent-follow state, desired distance, and any replaced "
            "follow session, plus task-agnostic natural-language recommended_next_actions for "
            "entity interaction or combat. The next action result records persistent_follow_stopped."
        ),
        "when_to_use": (
            "Use after scan_entities when a mobile non-combat target may move away while the model "
            "chooses the next action. For interactions, follow by entity_id and make use_item on the "
            "same entity_id the next action."
        ),
    },
    "dig_block_at": {
        "purpose": "Dig one block at an explicit coordinate.",
        "args": {
            "position": {"x": "number", "y": "number", "z": "number"},
            "block": "optional expected canonical block id used as a guard",
            "timeout_ms": "optional dig timeout",
            "drop_observation_ms": "optional 0-2000ms window for server-originated item-entity evidence",
        },
        "returns": (
            "block transition, held item, estimated dig time, inventory delta, newly server-observed "
            "drop entities, and a drop_observation_status that never overclaims an unobserved drop."
        ),
        "when_to_use": "Use when a concrete block coordinate is supported by prior evidence and the bot is close enough.",
    },
    "wait_ticks": {
        "purpose": "Wait for a short number of Minecraft ticks without issuing movement or world-changing actions.",
        "args": {"ticks": "optional number of ticks to wait, default 10"},
        "returns": "waited_ticks and a fresh observation.",
        "when_to_use": "Use after moving into pickup range or after an action whose effect may appear after a short delay.",
    },
    "craft_item": {
        "purpose": "Deprecated compatibility alias for process_item station=inventory or station=crafting_table.",
        "args": {
            "item": "canonical item id",
            "count": "desired output count",
            "station": "optional station",
        },
        "returns": "crafted item counts and station used.",
        "when_to_use": "Hidden from normal prompts; prefer process_item.",
    },
    "smelt_item": {
        "purpose": "Deprecated compatibility alias for process_item station=furnace.",
        "args": {
            "item": "canonical output item id, for example glass, iron_ingot, gold_ingot, stone, or cooked_beef",
            "input": "optional canonical input item id; inferred for common furnace recipes such as glass <- sand",
            "fuel": "optional canonical fuel item id; inferred from inventory when omitted",
            "count": "desired output count",
            "max_distance": "optional furnace search radius",
            "timeout_ms": "optional smelting timeout",
        },
        "returns": "input, fuel, output count, furnace position, inventory delta, and smelting duration.",
        "when_to_use": "Hidden from normal prompts; prefer process_item.",
    },
    "process_item": {
        "purpose": (
            "Process an item through a Minecraft workstation or inventory recipe. "
            "Use station=inventory for 2x2 crafting, station=crafting_table for 3x3 crafting, "
            "and station=furnace for smelting or cooking."
        ),
        "args": {
            "station": "inventory, crafting_table, nearby_crafting_table, or furnace",
            "output": "canonical output item id, for example oak_planks, wooden_pickaxe, glass, iron_ingot, or cooked_beef",
            "count": "desired output count",
            "input": "optional canonical furnace input item id; used for station=furnace",
            "fuel": "optional canonical fuel item id; used for station=furnace",
            "max_distance": "optional workstation search radius",
            "timeout_ms": "optional processing timeout",
        },
        "returns": "station, output, crafted or smelted counts, inventory delta, and station-specific diagnostics.",
        "when_to_use": (
            "Use after required ingredients and the required station are already available; "
            "do not use it to gather materials or place the station. If the station, input, fuel, or output count is uncertain, call get_recipe first."
        ),
    },
    "place_block": {
        "purpose": "Place one inventory block into the world.",
        "args": {"item": "canonical block item id", "position": "optional target coordinate"},
        "returns": "placement target and support block.",
        "when_to_use": "Use for build/place tasks or when navigation evidence shows a support block could make an otherwise unreachable nearby target reachable.",
    },
    "equip_item": {
        "purpose": "Equip one inventory item into a specific equipment slot without activating it or changing the world.",
        "args": {
            "item": "canonical item id, for example iron_sword, bow, shield, or diamond_helmet",
            "slot": "equipment slot: hand, off_hand, head, chest, legs, or feet",
            "timeout_ms": "optional equip timeout",
        },
        "returns": "equipped item, slot, previous equipment, and current equipment snapshot.",
        "when_to_use": "Use before combat or item interactions when the current observation shows the desired item is in inventory but not equipped.",
    },
    "use_item": {
        "purpose": "Use an equipped item, activate a block, or activate an entity.",
        "args": {
            "item": "optional item id",
            "block": "optional block id",
            "entity": "optional entity name",
            "entity_id": "optional numeric entity id returned by scan_entities",
            "effect_observation_ms": (
                "optional 0-2000ms post-interaction observation window; defaults to 750ms "
                "for entity metadata, inventory, and newly spawned drop evidence"
            ),
        },
        "returns": (
            "activation target; entity interactions also return the actual held item, entity_id, "
            "inventory delta, newly server-observed drops, named metadata changes, before/after "
            "server entity details, and whether any local effect was observed. This is local "
            "interaction evidence, not a global task-success judgment."
        ),
        "when_to_use": "Use for interaction tasks after moving near the target.",
    },
    "consume_item": {
        "purpose": "Consume food, drinkable potions, milk, or other consumables and wait for the health/food/effect state to update.",
        "args": {
            "item": "canonical consumable item id, for example cooked_beef, golden_apple, potion, or milk_bucket",
            "timeout_ms": "optional consume timeout",
        },
        "returns": "consumed item, inventory delta, health/food before and after, and whether the consume completed.",
        "when_to_use": "Use when health or food is low before continuing combat or long-running tasks.",
    },
    "move_to_and_engage_combat": {
        "purpose": (
            "Move toward, dynamically track, and attack one selected entity during a bounded real-time "
            "combat engagement using currently equipped gear."
        ),
        "args": {
            "entity": "target entity name",
            "mode": "melee or ranged",
            "weapon": "optional expected currently equipped hand item; use equip_item first if it differs",
            "ammo": "optional ammo item for ranged mode; must already be in inventory",
            "max_distance": "optional maximum target acquisition and tracking radius",
            "max_duration_ms": "optional engagement duration budget",
            "unreachable_timeout_ms": (
                "optional no-progress tracking interval before melee returns target_unreachable"
            ),
            "retreat_health": "optional health threshold for stopping and returning control",
        },
        "returns": (
            "combat status, attack/shot counts, dynamic tracking and current-engagement reachability "
            "evidence, health/food state, and confirmed entity-death events attributed to this bot."
        ),
        "when_to_use": (
            "Use after observing or scanning a mobile combat target when approach, tracking, and attacks "
            "should occur in one bounded action. This action does not equip weapons, choose melee versus "
            "ranged mode, or consume healing items; use equip_item or consume_item explicitly."
        ),
    },
    "engage_combat": {
        "purpose": "Deprecated compatibility alias for move_to_and_engage_combat.",
        "args": {"entity": "target entity name", "mode": "melee or ranged"},
        "returns": "The same bounded dynamic tracking and combat evidence as move_to_and_engage_combat.",
        "when_to_use": "Compatibility only; new model prompts hide this alias.",
    },
    "fight_entity": {
        "purpose": "Deprecated compatibility alias for bounded melee combat; prefer move_to_and_engage_combat.",
        "args": {
            "entity": "target entity name",
            "weapon": "optional weapon item",
            "max_attacks": "optional budget",
        },
        "returns": "attack count and defeated flag.",
        "when_to_use": "Use only for combat tasks with a visible or nearby target entity.",
    },
    "query_inventory": {
        "purpose": "Read the current inventory without changing the world.",
        "args": {},
        "returns": "inventory item counts.",
        "when_to_use": "Use to verify progress when the latest observation may be stale or incomplete.",
    },
    "execute_skill": {
        "purpose": "Execute a promoted skill from the skill library.",
        "args": {"name": "skill name", "version": "optional skill version"},
        "returns": "skill execution result.",
        "when_to_use": "Use only after a retrieved skill clearly matches the current task and is enabled.",
    },
    "request_visual_snapshot": {
        "purpose": "Request a visual frame when textual observation is insufficient.",
        "args": {},
        "returns": "snapshot metadata or unavailable reason.",
        "when_to_use": "Use when stuck, disoriented, or the task depends on visual layout.",
    },
    "submit_for_evaluation": {
        "purpose": (
            "Stop selecting Minecraft actions and submit the current run state as final. An "
            "authoritative evaluator decides success when configured; otherwise the result remains "
            "explicitly unverified. This stops execution; it does not declare success."
        ),
        "args": {},
        "returns": (
            "Whether the finish request was accepted, rejected by an online verifier, or accepted "
            "for an external evaluator such as MineCLIP. Without an evaluator, it terminates with "
            "task_success=null and evaluation_status=not_evaluated."
        ),
        "when_to_use": (
            "Use only when current observations and prior action results provide concrete evidence "
            "that the task goal is satisfied. If rejected, continue acting from the returned verifier "
            "evidence instead of immediately submitting again."
        ),
    },
}


class ToolRegistry:
    """Validated action registry exposed to the LLM."""

    def __init__(self, enabled_actions: Iterable[ActionType] | None = None) -> None:
        self._enabled_actions: set[ActionType] = set(enabled_actions or ())

    @property
    def enabled_actions(self) -> tuple[ActionType, ...]:
        """Return enabled action names in deterministic order for prompts and audit."""

        return tuple(sorted(self._enabled_actions))

    @property
    def prompt_visible_actions(self) -> tuple[ActionType, ...]:
        """Return enabled action names that should be shown to the model."""

        return tuple(
            action for action in self.enabled_actions if action not in PROMPT_HIDDEN_ACTIONS
        )

    def enable(self, action_type: ActionType) -> None:
        """Expose one action type to the current run."""

        self._enabled_actions.add(action_type)

    def enable_many(self, action_types: Iterable[ActionType]) -> None:
        """Expose multiple action types to the current run."""

        self._enabled_actions.update(action_types)

    def scoped(self, action_types: Iterable[ActionType]) -> "ToolRegistry":
        """Create a task-local registry without mutating the base registry."""

        return ToolRegistry(action_types)

    def validate(self, action: HarnessAction) -> HarnessAction:
        """Reject actions that are not in the active action scope."""

        if action.type not in self._enabled_actions:
            raise ValueError(f"Action is not enabled for this run: {action.type}")
        return action

    def action_guides(self) -> list[dict[str, object]]:
        """Return prompt-ready descriptions for currently enabled actions."""

        return [
            {"type": action_type, **ACTION_PRIMITIVE_GUIDE[action_type]}
            for action_type in self.prompt_visible_actions
            if action_type in ACTION_PRIMITIVE_GUIDE
        ]
