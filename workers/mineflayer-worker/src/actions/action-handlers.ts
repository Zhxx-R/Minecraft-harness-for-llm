import type { Bot } from "mineflayer";
import mineflayerPathfinder, {
  type ComputedPath,
  type PartiallyComputedPath
} from "mineflayer-pathfinder";
import type { Block } from "prismarine-block";
import { Vec3 } from "vec3";
import type { HarnessAction } from "./action-schemas.js";
import { observe } from "../observations/observe.js";
import {
  CombatReachabilityTracker,
  type CombatReachabilitySnapshot
} from "../runtime/combat-reachability.js";
import { combatTrackerFor } from "../runtime/combat-tracker.js";
import { isEntityAirborne } from "../runtime/entity-state.js";
import {
  entityMetadataDelta,
  entityServerDetails
} from "../runtime/entity-details.js";
import {
  startPersistentFollow,
  stopPersistentFollow
} from "../runtime/persistent-follow.js";

const { Movements, goals } = mineflayerPathfinder;

/** JSON-safe runtime result returned for every worker action. */
type ActionResult = Record<string, unknown>;

/** Minimal entity shape used by Mineflayer combat and activation actions. */
type RuntimeEntity = Bot["entity"];

/** Narrow Mineflayer furnace API shape used by the smelt_item primitive. */
type RuntimeFurnace = {
  putInput: (itemType: number, metadata: number | null, count: number) => Promise<void>;
  putFuel: (itemType: number, metadata: number | null, count: number) => Promise<void>;
  takeOutput: () => Promise<{ name?: string; count?: number } | null>;
  outputItem?: (() => { name?: string; count?: number } | null) | { name?: string; count?: number } | null;
  close: () => void;
};

/** Valid placement solution made from an air target and adjacent support block. */
type BlockPlacement = { reference: Block; face: Vec3; target: Vec3 };

/** Cardinal block directions used when placing blocks against a support block. */
const ADJACENT_DIRECTIONS = [
  new Vec3(1, 0, 0),
  new Vec3(-1, 0, 0),
  new Vec3(0, 1, 0),
  new Vec3(0, -1, 0),
  new Vec3(0, 0, 1),
  new Vec3(0, 0, -1)
] as const;

/** Pathfinder movement policy exposed through the single move_to primitive. */
const MOVEMENT_POLICY = {
  can_dig: true,
  can_place: true,
  place_policy: "safe_inventory_scaffold_blocks",
  allow_1x1_towers: true,
  allow_parkour: true,
  allow_sprinting: true,
  max_drop_down: 3
} as const;

/** Low-risk blocks that pathfinder may spend while bridging or climbing. */
const SAFE_SCAFFOLDING_ITEM_NAMES = new Set([
  "dirt",
  "coarse_dirt",
  "rooted_dirt",
  "cobblestone",
  "stone",
  "andesite",
  "diorite",
  "granite",
  "deepslate",
  "cobbled_deepslate",
  "netherrack",
  "sand",
  "red_sand",
  "gravel"
]);

/** Common furnace output-to-input recipes used by MineDojo furnace-core tasks. */
const FURNACE_INPUT_BY_OUTPUT = new Map([
  ["glass", "sand"],
  ["stone", "cobblestone"],
  ["smooth_stone", "stone"],
  ["iron_ingot", "raw_iron"],
  ["gold_ingot", "raw_gold"],
  ["copper_ingot", "raw_copper"],
  ["brick", "clay_ball"],
  ["nether_brick", "netherrack"],
  ["cooked_beef", "beef"],
  ["cooked_chicken", "chicken"],
  ["cooked_cod", "cod"],
  ["cooked_mutton", "mutton"],
  ["cooked_porkchop", "porkchop"],
  ["cooked_rabbit", "rabbit"],
  ["cooked_salmon", "salmon"],
  ["baked_potato", "potato"],
  ["charcoal", "oak_log"]
]);

/** Fuel candidates ordered by how likely they are to be intentionally provided by reset. */
const FURNACE_FUEL_ITEM_NAMES = [
  "coal",
  "charcoal",
  "coal_block",
  "lava_bucket",
  "oak_log",
  "spruce_log",
  "birch_log",
  "jungle_log",
  "acacia_log",
  "dark_oak_log",
  "mangrove_log",
  "cherry_log",
  "oak_planks",
  "spruce_planks",
  "birch_planks",
  "jungle_planks",
  "acacia_planks",
  "dark_oak_planks",
  "mangrove_planks",
  "cherry_planks"
];

/** Model-facing affordance hints when navigation cannot reach a target. */
const NAVIGATION_AFFORDANCES = [
  {
    action: "scan_blocks",
    when: "Use this after pathfinder fails to inspect nearby blocking terrain or choose another target."
  },
  {
    action: "dig_block_at",
    when: "Use this only if pathfinder cannot route and a specific nearby block still must be removed."
  },
  {
    action: "place_block",
    when: "Use this only if pathfinder has no safe scaffold blocks or the task needs explicit placement."
  },
  {
    action: "scan_dropped_items",
    when: "Use this if the target was a dropped item and its exact position may have changed."
  }
] as const;

/** Real-time combat controller constants kept inside the worker, not exposed as Mineflayer APIs. */
const COMBAT_TICK_MS = 100;
const COMBAT_TARGET_REFRESH_MS = 500;
const COMBAT_ATTACK_INTERVAL_MS = 650;
const COMBAT_MELEE_RANGE = 3.2;
const COMBAT_RANGED_PREFERRED_DISTANCE = 10;
const COMBAT_RANGED_DRAW_MS = 1200;
const COMBAT_UNREACHABLE_STALL_MS = 8000;
const MOVE_TIMEOUT_MIN_MS = 8000;
const MOVE_TIMEOUT_PER_BLOCK_MS = 350;
const MOVE_TIMEOUT_DEFAULT_MAX_MS = 60000;
const MOVE_TIMEOUT_EXPLICIT_MAX_MS = 90000;

/** Task-agnostic handoff guidance returned whenever persistent follow starts. */
const FOLLOW_RECOMMENDED_NEXT_ACTIONS = [
  "use_item: Use when the task requires using the held item on the followed entity.",
  "move_to_and_engage_combat: Use when the task requires attacking the followed entity."
] as const;

/** Items that can be used by the current bounded ranged-combat primitive. */
const RANGED_WEAPON_NAMES = new Set(["bow", "crossbow", "trident"]);

/** Common Minecraft consumables that Mineflayer can consume via bot.consume(). */
const CONSUMABLE_ITEM_NAMES = new Set([
  "apple",
  "baked_potato",
  "beef",
  "beetroot",
  "beetroot_soup",
  "bread",
  "carrot",
  "chicken",
  "cooked_beef",
  "cooked_chicken",
  "cooked_cod",
  "cooked_mutton",
  "cooked_porkchop",
  "cooked_rabbit",
  "cooked_salmon",
  "cookie",
  "dried_kelp",
  "enchanted_golden_apple",
  "golden_apple",
  "golden_carrot",
  "honey_bottle",
  "melon_slice",
  "milk_bucket",
  "mushroom_stew",
  "poisonous_potato",
  "porkchop",
  "potato",
  "potion",
  "pumpkin_pie",
  "rabbit",
  "rabbit_stew",
  "rotten_flesh",
  "spider_eye",
  "suspicious_stew",
  "sweet_berries",
  "tropical_fish"
]);

/** Dispatch one validated harness action to worker-side Mineflayer logic. */
export async function handleAction(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const stoppedFollow =
    action.type === "follow"
      ? null
      : stopPersistentFollow(bot, "next_action_received");
  const result = await dispatchAction(bot, action);
  return stoppedFollow
    ? {
        ...result,
        persistent_follow_stopped: stoppedFollow
      }
    : result;
}

/** Dispatch one action after persistent cross-turn movement has been reconciled. */
async function dispatchAction(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  switch (action.type) {
    case "query_inventory":
      return success(bot, action.type, {
        inventory: inventorySnapshot(bot)
      });
    case "request_visual_snapshot":
      return failure(
        bot,
        action.type,
        "visual_capture_unavailable",
        "Visual frame capture is not configured for this worker runtime.",
        false,
        {
          snapshot: {
            image: null,
            format: null,
            reason: "Use a backend VisualSnapshotRuntime with a configured frame provider."
          }
        }
      );
    case "scan_blocks":
      return await scanBlocks(bot, action);
    case "scan_entities":
      return await scanEntities(bot, action);
    case "scan_dropped_items":
      return await scanDroppedItems(bot, action);
    case "move_to":
      return await moveTo(bot, action);
    case "follow":
      return followEntity(bot, action);
    case "dig_block_at":
      return await digBlockAt(bot, action);
    case "wait_ticks":
      return await waitTicks(bot, action);
    case "process_item":
      return await processItem(bot, action);
    case "craft_item":
      return await craftItem(bot, action);
    case "smelt_item":
      return await smeltItem(bot, action);
    case "place_block":
      return await placeBlock(bot, action);
    case "equip_item":
      return await equipItem(bot, action);
    case "use_item":
      return await useItem(bot, action);
    case "consume_item":
      return await consumeItem(bot, action);
    case "move_to_and_engage_combat":
    case "engage_combat":
      return await engageCombat(bot, action);
    case "fight_entity":
      return await fightEntity(bot, action);
    default:
      return failure(bot, action.type, "unsupported_action", `Action not implemented: ${action.type}`, false);
  }
}

/** Process an item through inventory crafting, crafting table crafting, or furnace smelting. */
async function processItem(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const station = normalizeProcessStation(stringArg(action.args.station));
  const outputName = stringArg(action.args.output ?? action.args.item_id ?? action.args.item ?? action.args.name);
  if (!outputName) {
    return failure(bot, action.type, "invalid_args", "process_item requires args.output, args.item, args.item_id, or args.name.", false);
  }
  if (station === "furnace") {
    return await smeltItem(bot, {
      type: action.type,
      args: {
        ...action.args,
        item: outputName,
        station
      }
    });
  }
  return await craftItem(bot, {
    type: action.type,
    args: {
      ...action.args,
      item: outputName,
      station: station === "inventory" ? undefined : station
    }
  });
}

/** Scan visible nearby blocks without deciding which one should be harvested. */
async function scanBlocks(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const targetName = stringArg(action.args.block_id ?? action.args.block ?? action.args.name);
  const maxDistance = numberArg(action.args.max_distance, 12);
  const count = Math.max(1, Math.floor(numberArg(action.args.count, 16)));

  return await withTimeout(bot, action.type, numberArg(action.args.timeout_ms, 5000), async () => {
    const positions = bot.findBlocks({
      matching: (block) => !targetName || block.name === targetName,
      maxDistance,
      count
    });
    const blocks = positions
      .map((position) => bot.blockAt(position))
      .filter((block): block is Block => block !== null)
      .map((block) => ({
        name: block.name,
        position: vec3ToJson(block.position),
        distance: bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5)),
        can_dig: bot.canDigBlock(block)
      }));

    return success(bot, action.type, {
      query: targetName ?? null,
      max_distance: maxDistance,
      blocks
    });
  });
}

/** Scan nearby entities and expose combat affordances without attacking. */
async function scanEntities(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const targetEntityId = entityIdArg(action.args.entity_id);
  const targetName = stringArg(action.args.entity ?? action.args.name);
  const maxDistance = numberArg(action.args.max_distance, 16);
  const count = Math.max(1, Math.floor(numberArg(action.args.count, 16)));

  return await withTimeout(bot, action.type, numberArg(action.args.timeout_ms, 3000), async () => {
    const entities = (Object.values(bot.entities) as RuntimeEntity[])
      .filter((entity) => entity.id !== bot.entity.id)
      .filter((entity) =>
        targetEntityId !== null
          ? entity.id === targetEntityId
          : !targetName || entityMatches(entity, targetName)
      )
      .map((entity) => entityCombatEvidence(bot, entity))
      .filter((entity) => Number(entity.distance) <= maxDistance)
      .sort((left, right) => Number(left.distance) - Number(right.distance))
      .slice(0, count);

    return success(bot, action.type, {
      query: targetEntityId ?? targetName ?? null,
      max_distance: maxDistance,
      entities
    });
  });
}

/** Scan visible nearby dropped item entities without moving. */
async function scanDroppedItems(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const itemName = stringArg(action.args.item_id ?? action.args.item ?? action.args.name);
  const maxDistance = numberArg(action.args.max_distance, 8);
  const count = Math.max(1, Math.floor(numberArg(action.args.count, 16)));

  return await withTimeout(bot, action.type, numberArg(action.args.timeout_ms, 3000), async () => {
    const items = (Object.values(bot.entities) as RuntimeEntity[])
      .filter((entity) => entity.id !== bot.entity.id)
      .filter((entity) => isDroppedItemEntity(entity))
      .filter((entity) => !itemName || entity.getDroppedItem?.()?.name === itemName)
      .map((entity) => {
        const dropped = entity.getDroppedItem?.();
        return {
          entity_id: entity.id,
          item: dropped?.name ?? null,
          count: dropped?.count ?? null,
          position: vec3ToJson(entity.position),
          distance: bot.entity.position.distanceTo(entity.position)
        };
      })
      .filter((entity) => entity.distance <= maxDistance)
      .sort((left, right) => left.distance - right.distance)
      .slice(0, count);

    return success(bot, action.type, {
      query: itemName ?? null,
      max_distance: maxDistance,
      dropped_items: items
    });
  });
}

/** Move using Mineflayer pathfinder as the worker-owned navigation controller. */
async function moveTo(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const target = vectorArg(action.args.position) ?? vectorFromArgs(action.args);
  const tolerance = numberArg(action.args.tolerance, 1.5);
  if (!target) {
    return failure(bot, action.type, "invalid_args", "move_to requires args.position or args.x/y/z.", false);
  }
  const startPosition = bot.entity.position.clone();
  const initialDistance = startPosition.distanceTo(target);
  const timeoutMs = movementTimeoutMs(action.args.timeout_ms, initialDistance);
  const planningTimeoutMs = movementPlanningTimeoutMs(timeoutMs);

  const movements = navigationMovements(bot);
  const scaffoldingItemNames = scaffoldingNames(bot, movements.scafoldingBlocks);
  const availableScaffoldingCount = movements.countScaffoldingItems();
  const inventoryBefore = inventoryCounts(bot);
  const goal = new goals.GoalNear(target.x, target.y, target.z, tolerance);
  let precomputedPath: ComputedPath | null = null;
  let lastPathUpdate: PartiallyComputedPath | null = null;
  const pathResets: string[] = [];
  const onPathUpdate = (path: PartiallyComputedPath) => {
    lastPathUpdate = path;
  };
  const onPathReset = (reason: string) => {
    pathResets.push(reason);
  };
  bot.on("path_update", onPathUpdate);
  bot.on("path_reset", onPathReset);
  try {
    bot.pathfinder.setMovements(movements);
    bot.pathfinder.thinkTimeout = planningTimeoutMs;
    precomputedPath = bot.pathfinder.getPathTo(movements, goal, planningTimeoutMs);
    if (precomputedPath.status === "noPath") {
      return navigationFailure(
        bot,
        action.type,
        target,
        tolerance,
        timeoutMs,
        "no_path",
        "Mineflayer pathfinder could not find a route under the current dig/place movement policy.",
        precomputedPath,
        pathResets,
        startPosition,
        initialDistance,
        undefined,
        scaffoldingItemNames,
        availableScaffoldingCount,
        planningTimeoutMs,
        inventoryBefore
      );
    }
    await withNavigationTimeout(bot, bot.pathfinder.goto(goal), timeoutMs);
  } catch (error) {
    const errorName = error instanceof Error ? error.name : "";
    const errorCode = navigationErrorCode(errorName);
    return navigationFailure(
      bot,
      action.type,
      target,
      tolerance,
      timeoutMs,
      errorCode,
      navigationDiagnosis(errorCode),
      lastPathUpdate ?? precomputedPath,
      pathResets,
      startPosition,
      initialDistance,
      errorToString(error),
      scaffoldingItemNames,
      availableScaffoldingCount,
      planningTimeoutMs,
      inventoryBefore
    );
  } finally {
    bot.removeListener("path_update", onPathUpdate);
    bot.removeListener("path_reset", onPathReset);
    bot.pathfinder.setGoal(null);
    clearMovement(bot);
  }

  const endPosition = bot.entity.position.clone();
  const finalDistance = endPosition.distanceTo(target);
  const pathDiagnostics = pathSummary(lastPathUpdate ?? precomputedPath);
  const inventoryAfter = inventoryCounts(bot);
  const inventoryDelta = inventoryNetDeltaCounts(inventoryBefore, inventoryAfter);
  const scaffoldingDelta = filteredDeltaCounts(inventoryDelta, scaffoldingItemNames);
  return success(bot, action.type, {
    start_position: vec3ToJson(startPosition),
    target: vec3ToJson(target),
    target_position: vec3ToJson(target),
    end_position: vec3ToJson(endPosition),
    tolerance,
    timeout_ms: timeoutMs,
    planning_timeout_ms: planningTimeoutMs,
    initial_distance: initialDistance,
    final_distance: finalDistance,
    distance_delta: initialDistance - finalDistance,
    distance: finalDistance,
    reached_tolerance: finalDistance <= tolerance,
    progress_status: finalDistance <= tolerance ? "reached" : "partial_progress",
    movement_policy: MOVEMENT_POLICY,
    scaffolding_item_names: scaffoldingItemNames,
    available_scaffolding_count: availableScaffoldingCount,
    inventory_before: inventoryBefore,
    inventory_after: inventoryAfter,
    inventory_delta: inventoryDelta,
    consumed_items: consumedItemCounts(inventoryDelta),
    scaffolding_delta: scaffoldingDelta,
    scaffolding_consumed: consumedItemCounts(scaffoldingDelta),
    path_summary: pathDiagnostics,
    requires_break_count: pathDiagnostics?.requires_break_count ?? 0,
    requires_place_count: pathDiagnostics?.requires_place_count ?? 0,
    has_parkour: pathDiagnostics?.has_parkour ?? false,
    path_resets: pathResets
  });
}

/**
 * Start following one loaded entity and return immediately.
 *
 * GoalFollow remains active while the harness observes state and waits for the
 * model's next decision. handleAction stops it synchronously when that next
 * action reaches the worker.
 */
function followEntity(bot: Bot, action: HarnessAction): ActionResult {
  const entityId = entityIdArg(action.args.entity_id);
  const entityName = stringArg(
    action.args.entity ?? action.args.name ?? action.args.entity_id
  );
  const maxDistance = numberArg(action.args.max_distance, 128);
  const followDistance = Math.max(
    0.5,
    numberArg(
      action.args.follow_distance ?? action.args.distance ?? action.args.tolerance,
      1.25
    )
  );
  if (entityId === null && !entityName) {
    return failure(
      bot,
      action.type,
      "invalid_args",
      "follow requires args.entity_id, args.entity, or args.name.",
      false
    );
  }

  const target =
    entityId !== null
      ? findEntityById(bot, entityId, maxDistance)
      : findNearestEntity(bot, entityName as string, maxDistance);
  if (!target) {
    return failure(
      bot,
      action.type,
      "target_not_found",
      `No matching entity is loaded within ${maxDistance} blocks.`,
      true,
      {
        entity_id: entityId,
        entity: entityName,
        max_distance: maxDistance,
        suggested_next_actions: ["scan_entities"]
      }
    );
  }

  bot.pathfinder.setMovements(navigationMovements(bot));
  const lifecycle = startPersistentFollow(
    bot,
    {
      id: target.id,
      name: String(target.name ?? target.type),
      type: String(target.type),
      position: target.position
    },
    followDistance,
    new goals.GoalFollow(target, followDistance)
  );
  return success(bot, action.type, {
    status: "following",
    persistent: true,
    until: "next_action_received",
    target: entityCombatEvidence(bot, target),
    follow_distance: followDistance,
    max_distance: maxDistance,
    recommended_next_actions: FOLLOW_RECOMMENDED_NEXT_ACTIONS,
    ...lifecycle
  });
}

/** Wait briefly for world state changes such as item pickup. */
async function waitTicks(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const ticks = Math.min(200, Math.max(1, Math.floor(numberArg(action.args.ticks, 10))));
  const timeoutMs = numberArg(action.args.timeout_ms, ticks * 75 + 500);
  return await withTimeout(bot, action.type, timeoutMs, async () => {
    await wait(ticks * 50);
    return success(bot, action.type, { waited_ticks: ticks, waited_ms: ticks * 50 });
  });
}

/** Dig the block at an explicit coordinate; planning and target choice stay outside the worker. */
async function digBlockAt(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const target = vectorArg(action.args.position) ?? vectorFromArgs(action.args);
  const expectedName = stringArg(action.args.block_id ?? action.args.block ?? action.args.name);
  const timeoutMs = numberArg(action.args.timeout_ms, 12000);
  const dropObservationMs = Math.max(0, Math.min(2000, numberArg(action.args.drop_observation_ms, 500)));
  if (!target) {
    return failure(bot, action.type, "invalid_args", "dig_block_at requires args.position or args.x/y/z.", false);
  }

  const block = bot.blockAt(target);
  if (!block || block.name === "air") {
    return failure(bot, action.type, "target_not_found", "No non-air block exists at the requested position.", true, {
      position: vec3ToJson(target),
      expected_block: expectedName
    });
  }
  if (expectedName && block.name !== expectedName) {
    return failure(bot, action.type, "unexpected_block", `Expected ${expectedName}, found ${block.name}.`, true, {
      position: vec3ToJson(target),
      expected_block: expectedName,
      actual_block: block.name
    });
  }
  if (!bot.canDigBlock(block)) {
    return failure(bot, action.type, "not_diggable", `Block is not currently diggable: ${block.name}.`, true, {
      position: vec3ToJson(block.position),
      block: block.name,
      distance: bot.entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5))
    });
  }

  const heldItem = bot.heldItem?.name ?? null;
  const estimatedDigTimeMs = block.digTime(bot.heldItem?.type ?? null, false, false, false);
  const dropEntityIdsBefore = droppedItemEntityIds(bot);
  return await withTimeout(bot, action.type, timeoutMs, async () => {
    const inventoryBefore = inventoryCounts(bot);
    const blockBefore = block.name;
    await bot.dig(block);
    if (dropObservationMs > 0) {
      await wait(dropObservationMs);
    }
    const inventoryAfter = inventoryCounts(bot);
    const inventoryDelta = inventoryDeltaCounts(inventoryBefore, inventoryAfter);
    const spawnedDrops = newlyObservedDroppedItems(
      bot,
      dropEntityIdsBefore,
      block.position,
      5
    );
    const dropObservationStatus = Object.keys(inventoryDelta).length > 0
      ? "inventory_gained"
      : spawnedDrops.length > 0
        ? "drop_entity_observed"
        : "no_drop_observed";
    return success(bot, action.type, {
      block: blockBefore,
      position: vec3ToJson(block.position),
      block_before: blockBefore,
      block_after: bot.blockAt(block.position)?.name ?? null,
      block_removed: bot.blockAt(block.position)?.name === "air",
      held_item: heldItem,
      estimated_dig_time_ms: estimatedDigTimeMs,
      inventory_delta: inventoryDelta,
      spawned_drops: spawnedDrops,
      drop_observation_status: dropObservationStatus,
      drop_observation_ms: dropObservationMs,
      drop_evidence_source: "minecraft_server_entity_packets_and_inventory"
    });
  }, () => ({
    block: block.name,
    position: vec3ToJson(block.position),
    block_after: bot.blockAt(block.position)?.name ?? null,
    block_removed: bot.blockAt(block.position)?.name === "air",
    held_item: heldItem,
    estimated_dig_time_ms: estimatedDigTimeMs,
    drop_observation_status: "dig_incomplete_no_drop_claim",
    drop_evidence_source: "minecraft_server_entity_packets_and_inventory"
  }), () => bot.stopDigging());
}

/** Craft an item using inventory recipes or a nearby crafting table when requested. */
async function craftItem(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const itemName = stringArg(action.args.item_id ?? action.args.item ?? action.args.name);
  const desiredCount = Math.max(1, Math.floor(numberArg(action.args.count, 1)));
  const station = stringArg(action.args.station);
  const timeoutMs = numberArg(action.args.timeout_ms, 12000);
  if (!itemName) {
    return failure(bot, action.type, "invalid_args", "craft_item requires args.item_id, args.item, or args.name.", false);
  }

  return await withTimeout(bot, action.type, timeoutMs, async () => {
    const registryItem = bot.registry.itemsByName[itemName];
    if (!registryItem) {
      return failure(bot, action.type, "unknown_item", `Unknown craft target item: ${itemName}.`, false, {
        item: itemName
      });
    }

    const craftingTable =
      station === "crafting_table" || station === "nearby_crafting_table"
        ? findNearestBlock(bot, "crafting_table", numberArg(action.args.max_distance, 6))
        : null;
    if ((station === "crafting_table" || station === "nearby_crafting_table") && !craftingTable) {
      return failure(bot, action.type, "missing_station", "No nearby crafting_table found.", true, {
        item: itemName,
        station
      });
    }

    const recipes = bot.recipesFor(registryItem.id, null, 1, craftingTable);
    const recipe = recipes[0];
    if (!recipe) {
      return failure(bot, action.type, "recipe_not_available", `No available recipe for ${itemName}.`, true, {
        item: itemName,
        station: station ?? "inventory"
      });
    }

    const producedPerCraft = Math.max(1, Math.floor(Number(recipe.result.count ?? 1)));
    const craftCount = Math.max(1, Math.ceil(desiredCount / producedPerCraft));
    await bot.craft(recipe, craftCount, craftingTable ?? undefined);
    return success(bot, action.type, {
      item: itemName,
      count: desiredCount,
      craft_count: craftCount,
      produced_per_craft: producedPerCraft,
      expected_output_count: craftCount * producedPerCraft,
      station: station ?? "inventory"
    });
  });
}

/** Smelt one output item with a nearby placed furnace, inventory input, and inventory fuel. */
async function smeltItem(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const outputName = stringArg(action.args.item_id ?? action.args.item ?? action.args.output ?? action.args.name);
  const desiredCount = Math.max(1, Math.floor(numberArg(action.args.count, 1)));
  const inputName = stringArg(action.args.input ?? action.args.input_item) ?? smeltingInputForOutput(outputName);
  const fuelName = stringArg(action.args.fuel ?? action.args.fuel_item) ?? firstAvailableFuelName(bot);
  const maxDistance = numberArg(action.args.max_distance, 6);
  const timeoutMs = numberArg(action.args.timeout_ms, Math.min(120000, 20000 + desiredCount * 12000));
  if (!outputName) {
    return failure(bot, action.type, "invalid_args", `${action.type} requires args.item, args.item_id, args.output, or args.name.`, false);
  }
  if (!inputName) {
    return failure(bot, action.type, "unknown_smelting_input", `No default furnace input is known for ${outputName}; provide args.input.`, true, {
      item: outputName,
      suggested_next_actions: ["retrieve_docs", "query_inventory"]
    });
  }
  if (!fuelName) {
    return failure(bot, action.type, "missing_fuel", "Inventory does not contain a known furnace fuel item.", true, {
      item: outputName,
      input: inputName,
      suggested_next_actions: ["query_inventory", "retrieve_docs"]
    });
  }

  return await withTimeout(bot, action.type, timeoutMs, async () => {
    const furnaceBlock = findNearestBlock(bot, "furnace", maxDistance);
    if (!furnaceBlock) {
      return failure(bot, action.type, "missing_station", "No nearby placed furnace found.", true, {
        item: outputName,
        input: inputName,
        fuel: fuelName,
        max_distance: maxDistance,
        suggested_next_actions: ["place_block", "query_inventory"]
      });
    }

    const inputItem = findInventoryItem(bot, inputName);
    if (!inputItem || inputItem.count < desiredCount) {
      return failure(bot, action.type, "missing_input", `Inventory does not contain enough ${inputName}.`, true, {
        item: outputName,
        input: inputName,
        required_count: desiredCount,
        available_count: inputItem?.count ?? 0,
        suggested_next_actions: ["scan_blocks", "move_to", "dig_block_at", "query_inventory"]
      });
    }
    const fuelItem = findInventoryItem(bot, fuelName);
    const fuelCount = Math.max(1, Math.ceil(desiredCount / 8));
    if (!fuelItem || fuelItem.count < fuelCount) {
      return failure(bot, action.type, "missing_fuel", `Inventory does not contain enough ${fuelName}.`, true, {
        item: outputName,
        input: inputName,
        fuel: fuelName,
        required_fuel_count: fuelCount,
        available_fuel_count: fuelItem?.count ?? 0,
        suggested_next_actions: ["query_inventory", "retrieve_docs"]
      });
    }

    const inventoryBefore = inventoryCounts(bot);
    const started = Date.now();
    let furnace: RuntimeFurnace | null = null;
    try {
      furnace = (await bot.openFurnace(furnaceBlock)) as RuntimeFurnace;
      await furnace.putInput(inputItem.type, inputItem.metadata ?? null, desiredCount);
      await furnace.putFuel(fuelItem.type, fuelItem.metadata ?? null, fuelCount);
      const outputCount = await takeFurnaceOutput(furnace, outputName, desiredCount, Math.max(1000, timeoutMs - 4000));
      const inventoryAfter = inventoryCounts(bot);
      if (outputCount < desiredCount) {
        return failure(bot, action.type, "smelting_timeout", `Furnace did not produce ${desiredCount} ${outputName} before timeout.`, true, {
          item: outputName,
          input: inputName,
          fuel: fuelName,
          count: desiredCount,
          output_count: outputCount,
          furnace_position: vec3ToJson(furnaceBlock.position),
          duration_ms: Date.now() - started,
          inventory_delta: inventoryNetDeltaCounts(inventoryBefore, inventoryAfter),
          suggested_next_actions: ["wait_ticks", "query_inventory", "process_item"]
        });
      }
      return success(bot, action.type, {
        item: outputName,
        input: inputName,
        fuel: fuelName,
        count: desiredCount,
        output_count: outputCount,
        furnace_position: vec3ToJson(furnaceBlock.position),
        duration_ms: Date.now() - started,
        inventory_delta: inventoryNetDeltaCounts(inventoryBefore, inventoryAfter)
      });
    } finally {
      furnace?.close();
    }
  });
}

/** Place one inventory block either at a target position or on the block below the bot. */
async function placeBlock(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const itemName = stringArg(action.args.item_id ?? action.args.item ?? action.args.block ?? action.args.name);
  const timeoutMs = numberArg(action.args.timeout_ms, 10000);
  if (!itemName) {
    return failure(bot, action.type, "invalid_args", "place_block requires args.item_id, args.item, args.block, or args.name.", false);
  }

  return await withTimeout(bot, action.type, timeoutMs, async () => {
    const inventoryItem = findInventoryItem(bot, itemName);
    if (!inventoryItem) {
      return failure(bot, action.type, "missing_item", `Inventory does not contain ${itemName}.`, true, {
        item: itemName
      });
    }

    await bot.equip(inventoryItem, "hand");
    const target = vectorArg(action.args.position) ?? vectorFromArgs(action.args);
    const placement = target
      ? findPlacementAgainstTarget(bot, target)
      : findNearbyPlacement(bot, numberArg(action.args.placement_radius, 3)) ?? findPlacementBelowBot(bot);
    if (!placement) {
      const placementRadius = numberArg(action.args.placement_radius, 3);
      const nearbyPlacements = findNearbyPlacements(bot, placementRadius, 5).map(placementToJson);
      return failure(bot, action.type, "no_support_block", "No valid support block found for placement.", true, {
        item: itemName,
        target: target ? vec3ToJson(target) : null,
        current_position: vec3ToJson(bot.entity.position),
        nearby_valid_placements: nearbyPlacements,
        placement_policy: {
          explicit_position_rule: "The target position must be empty and adjacent to a solid support block.",
          default_search_rule: "If position is omitted, the worker searches nearby air blocks with support.",
          open_area_strategy: "Move to a nearby flat/open area or next to a visible solid ground block, then retry place_block."
        },
        suggested_next_actions: ["place_block", "move_to", "scan_blocks"],
        recovery_hint:
          nearbyPlacements.length > 0
            ? "This is a placement-geometry problem, not a recipe or inventory problem. A nearby valid placement exists; retry place_block without position, or use one nearby_valid_placements target."
            : "This is a placement-geometry problem, not a recipe or inventory problem. No nearby support placement was found; move to a flatter/open area with solid ground, scan nearby blocks, then retry place_block without position."
      });
    }

    await bot.placeBlock(placement.reference, placement.face);
    return success(bot, action.type, {
      item: itemName,
      target: vec3ToJson(placement.target),
      reference: vec3ToJson(placement.reference.position),
      face: vec3ToJson(placement.face)
    });
  });
}

/** Equip one inventory item into an equipment slot without using or activating it. */
async function equipItem(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const itemName = stringArg(action.args.item_id ?? action.args.item ?? action.args.name);
  const destination = normalizeEquipmentDestination(
    stringArg(action.args.slot ?? action.args.destination ?? action.args.equipment_slot)
  );
  const publicSlot = publicEquipmentSlot(destination);
  const timeoutMs = numberArg(action.args.timeout_ms, 8000);
  if (!itemName) {
    return failure(bot, action.type, "invalid_args", "equip_item requires args.item_id, args.item, or args.name.", false);
  }

  return await withTimeout(bot, action.type, timeoutMs, async () => {
    const inventoryItem = findInventoryItem(bot, itemName);
    if (!inventoryItem) {
      return failure(bot, action.type, "missing_item", `Inventory does not contain ${itemName}.`, true, {
        item: itemName,
        slot: publicSlot,
        equipment: equipmentSnapshotForAction(bot),
        suggested_next_actions: ["query_inventory"]
      });
    }

    const equipmentBefore = equipmentSnapshotForAction(bot);
    await bot.equip(inventoryItem, destination as never);
    return success(bot, action.type, {
      item: itemName,
      slot: publicSlot,
      destination,
      equipment_before: equipmentBefore,
      equipment_after: equipmentSnapshotForAction(bot)
    });
  });
}

/** Use an equipped item, activate a nearby block, or activate a nearby entity. */
async function useItem(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const itemName = stringArg(action.args.item_id ?? action.args.item ?? action.args.name);
  const blockName = stringArg(action.args.block_id ?? action.args.block);
  const entityId = entityIdArg(action.args.entity_id);
  const entityName = stringArg(action.args.entity ?? action.args.entity_id);
  const maxDistance = numberArg(action.args.max_distance, 5);
  const timeoutMs = numberArg(action.args.timeout_ms, 8000);
  const settleMs = Math.min(
    2000,
    Math.max(0, numberArg(action.args.effect_observation_ms ?? action.args.settle_ms, 750))
  );

  return await withTimeout(bot, action.type, timeoutMs, async () => {
    if (itemName) {
      const inventoryItem = findInventoryItem(bot, itemName);
      if (!inventoryItem) {
        return failure(bot, action.type, "missing_item", `Inventory does not contain ${itemName}.`, true, {
          item: itemName
        });
      }
      await bot.equip(inventoryItem, "hand");
    }

    if (blockName) {
      const block = findNearestBlock(bot, blockName, maxDistance);
      if (!block) {
        return failure(bot, action.type, "target_not_found", `No nearby ${blockName} block found.`, true, {
          block: blockName
        });
      }
      await bot.activateBlock(block);
      return success(bot, action.type, { activated: "block", block: blockName, item: itemName });
    }

    if (entityId !== null || entityName) {
      const entity =
        entityId !== null
          ? findEntityById(bot, entityId, maxDistance)
          : findNearestEntity(bot, entityName as string, maxDistance);
      if (!entity) {
        return failure(
          bot,
          action.type,
          "target_not_found",
          `No nearby ${entityName ?? `entity ${entityId}`} found.`,
          true,
          {
            entity_id: entityId,
            entity: entityName
          }
        );
      }
      const heldItem = bot.heldItem?.name ?? null;
      const inventoryBefore = inventoryCounts(bot);
      const dropEntityIdsBefore = droppedItemEntityIds(bot);
      const targetPosition = entity.position.clone();
      const targetDetailsBefore = entityServerDetails(bot, entity);
      bot.activateEntity(entity);
      if (settleMs > 0) {
        await wait(settleMs);
      }
      const inventoryAfter = inventoryCounts(bot);
      const inventoryDelta = inventoryDeltaCounts(inventoryBefore, inventoryAfter);
      const currentTarget = (bot.entities[entity.id] as RuntimeEntity | undefined) ?? entity;
      const targetDetailsAfter = entityServerDetails(bot, currentTarget);
      const metadataDelta = entityMetadataDelta(targetDetailsBefore, targetDetailsAfter);
      const spawnedDrops = newlyObservedDroppedItemsNearEntity(
        bot,
        dropEntityIdsBefore,
        targetPosition,
        6
      );
      return success(bot, action.type, {
        activated: "entity",
        entity_id: entity.id,
        entity: entity.name ?? entity.type,
        item: itemName ?? heldItem,
        held_item: heldItem,
        inventory_delta: inventoryDelta,
        spawned_drops: spawnedDrops,
        metadata_delta: metadataDelta,
        target_details_before: targetDetailsBefore,
        target_details_after: targetDetailsAfter,
        effect_observation_ms: settleMs,
        observed_effect:
          Object.keys(inventoryDelta).length > 0 ||
          spawnedDrops.length > 0 ||
          Object.keys(metadataDelta).length > 0,
        effect_evidence_source:
          "minecraft_server_entity_packets_metadata_and_inventory"
      });
    }

    bot.activateItem();
    return success(bot, action.type, { activated: "item", item: itemName ?? null });
  });
}

/** Consume food, drinkable potions, milk, or similar items and wait for state changes. */
async function consumeItem(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const itemName = stringArg(action.args.item_id ?? action.args.item ?? action.args.name);
  const timeoutMs = numberArg(action.args.timeout_ms, 12000);
  if (!itemName) {
    return failure(bot, action.type, "invalid_args", "consume_item requires args.item_id, args.item, or args.name.", false);
  }

  return await withTimeout(bot, action.type, timeoutMs, async () => {
    const item = findInventoryItem(bot, itemName);
    if (!item) {
      return failure(bot, action.type, "missing_item", `Inventory does not contain consumable ${itemName}.`, true, {
        item: itemName
      });
    }
    if (!CONSUMABLE_ITEM_NAMES.has(itemName)) {
      return failure(bot, action.type, "not_consumable", `${itemName} is not in the known consumable item set.`, true, {
        item: itemName
      });
    }

    const healthBefore = bot.health;
    const foodBefore = bot.food;
    const inventoryBefore = inventoryCounts(bot);
    await bot.equip(item, "hand");
    await bot.consume();
    await wait(numberArg(action.args.settle_ms, 250));
    const inventoryAfter = inventoryCounts(bot);
    return success(bot, action.type, {
      item: itemName,
      consumed: true,
      health_before: healthBefore,
      health_after: bot.health,
      health_delta: bot.health - healthBefore,
      food_before: foodBefore,
      food_after: bot.food,
      food_delta: bot.food - foodBefore,
      inventory_delta: inventoryDeltaCounts(inventoryBefore, inventoryAfter)
    });
  });
}

/** Compatibility wrapper for older tasks; prefer move_to_and_engage_combat in prompts. */
async function fightEntity(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const entityName = stringArg(action.args.entity_id ?? action.args.entity ?? action.args.name);
  const weaponName = stringArg(action.args.weapon);
  if (!entityName) {
    return failure(bot, action.type, "invalid_args", "fight_entity requires args.entity_id, args.entity, or args.name.", false);
  }

  const result = await engageCombat(bot, {
    type: "move_to_and_engage_combat",
    args: {
      entity: entityName,
      weapon: weaponName,
      mode: "melee",
      max_distance: action.args.max_distance,
      max_duration_ms: action.args.timeout_ms,
      retreat_health: action.args.retreat_health,
      stop_on_kill: true
    }
  });
  return {
    ...result,
    action_type: action.type,
    compatibility_action_type: "move_to_and_engage_combat",
    defeated: result.status === "target_killed",
    attacks: result.attacks ?? 0
  };
}

/** Run one bounded worker-side combat engagement and return tactical evidence. */
async function engageCombat(bot: Bot, action: HarnessAction): Promise<ActionResult> {
  const entityName = stringArg(action.args.entity_id ?? action.args.entity ?? action.args.name);
  const mode = stringArg(action.args.mode) === "ranged" ? "ranged" : "melee";
  const weaponName = stringArg(action.args.weapon);
  const ammoName = stringArg(action.args.ammo);
  const maxDistance = numberArg(action.args.max_distance, mode === "ranged" ? 24 : 16);
  const maxDurationMs = numberArg(action.args.max_duration_ms ?? action.args.timeout_ms, mode === "ranged" ? 45000 : 30000);
  const unreachableTimeoutMs = Math.max(
    1000,
    Math.min(maxDurationMs, numberArg(action.args.unreachable_timeout_ms, COMBAT_UNREACHABLE_STALL_MS))
  );
  const retreatHealth = numberArg(action.args.retreat_health, 6);
  const stopOnKill = booleanArg(action.args.stop_on_kill, true);
  if (!entityName) {
    return failure(bot, action.type, "invalid_args", `${action.type} requires args.entity_id, args.entity, or args.name.`, false);
  }

  return await withTimeout(bot, action.type, maxDurationMs + 2500, async () => {
    const weaponResult = validateCombatEquipment(bot, mode, weaponName, ammoName);
    if (!weaponResult.ok) {
      return combatFailure(bot, action.type, entityName, mode, String(weaponResult.status), String(weaponResult.message), {
        weapon: weaponName ?? null,
        current_weapon: weaponResult.current_weapon,
        ammo: ammoName ?? null,
        equipment: weaponResult.equipment,
        suggested_modes: weaponResult.suggested_modes,
        suggested_next_actions: weaponResult.suggested_next_actions
      });
    }

    const combatTracker = combatTrackerFor(bot);
    const initialKillCount = combatTracker.killCount(entityName);
    const killEventCursor = combatTracker.currentSequence();
    const started = Date.now();
    const reachabilityTracker = new CombatReachabilityTracker(started, unreachableTimeoutMs);
    const events: Record<string, unknown>[] = [];
    let attacks = 0;
    let shots = 0;
    let lastTarget: RuntimeEntity | null = null;
    let lastTargetRefresh = 0;
    let lastAttackAt = 0;
    let lastPathUpdate = 0;
    let lastAirborneEventAt = 0;
    let trackedTargetId: number | null = null;

    bot.pathfinder.setMovements(navigationMovements(bot));
    try {
      while (Date.now() - started < maxDurationMs) {
        const killDelta = combatTracker.killCount(entityName) - initialKillCount;
        if (stopOnKill && killDelta > 0) {
          return combatSuccess(bot, action.type, entityName, mode, "target_killed", started, {
            attacks,
            shots,
            weapon: weaponResult.weapon,
            equipment: weaponResult.equipment,
            kill_stat_delta: killDelta,
            confirmed_kill_delta: killDelta,
            kill_count_source: "mineflayer_entity_dead",
            kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
            combat_events: boundedCombatEvents(events),
            target: lastTarget ? entityCombatEvidence(bot, lastTarget) : null
          });
        }
        if (bot.health <= retreatHealth) {
          return combatFailure(bot, action.type, entityName, mode, "low_health", "Health is below the configured retreat threshold.", {
            attacks,
            shots,
            weapon: weaponResult.weapon,
            equipment: weaponResult.equipment,
            retreat_health: retreatHealth,
            combat_events: boundedCombatEvents(events),
            suggested_next_actions: ["consume_item", "move_to", "move_to_and_engage_combat"]
          });
        }

        const now = Date.now();
        if (!lastTarget || now - lastTargetRefresh >= COMBAT_TARGET_REFRESH_MS) {
          lastTarget =
            trackedTargetId === null
              ? findNearestEntity(bot, entityName, maxDistance)
              : findEntityById(bot, trackedTargetId, maxDistance);
          lastTargetRefresh = now;
          if (lastTarget) {
            trackedTargetId = lastTarget.id;
            events.push({ event: "target_acquired", at_ms: now - started, target: entityCombatEvidence(bot, lastTarget) });
          }
        }

        if (!lastTarget) {
          // Let entityDead settle before classifying disappearance as target_lost.
          await wait(COMBAT_TICK_MS);
          const settledKillDelta = combatTracker.killCount(entityName) - initialKillCount;
          if (stopOnKill && settledKillDelta > 0) {
            return combatSuccess(bot, action.type, entityName, mode, "target_killed", started, {
              attacks,
              shots,
              weapon: weaponResult.weapon,
              equipment: weaponResult.equipment,
              kill_stat_delta: settledKillDelta,
              confirmed_kill_delta: settledKillDelta,
              kill_count_source: "mineflayer_entity_dead",
              kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
              combat_events: boundedCombatEvents(events),
              target: null
            });
          }
          return combatFailure(bot, action.type, entityName, mode, "target_lost", "No matching target entity is visible within combat range.", {
            attacks,
            shots,
            weapon: weaponResult.weapon,
            equipment: weaponResult.equipment,
            max_distance: maxDistance,
            tracked_target_id: trackedTargetId,
            confirmed_kill_delta: settledKillDelta,
            kill_count_source: "mineflayer_entity_dead",
            kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
            combat_events: boundedCombatEvents(events),
            suggested_next_actions: ["scan_entities", "move_to"]
          });
        }

        const evidence = entityCombatEvidence(bot, lastTarget);
        reachabilityTracker.observe(
          {
            distance: Number(evidence.distance),
            heightDelta: Number(evidence.height_delta),
            targetAirborne: Boolean(evidence.target_airborne),
            meleeReachable: Boolean(evidence.melee_reachable)
          },
          now
        );
        if (mode === "melee") {
          if (evidence.target_airborne && now - lastAirborneEventAt >= 1000) {
            events.push({
              event: "target_temporarily_airborne",
              at_ms: now - started,
              target: evidence,
              decision: "continue_following_until_landing_or_timeout"
            });
            lastAirborneEventAt = now;
          }
          if (!evidence.melee_reachable) {
            if (now - lastPathUpdate >= COMBAT_TARGET_REFRESH_MS) {
              bot.pathfinder.setGoal(new goals.GoalFollow(lastTarget, 2), true);
              lastPathUpdate = now;
              events.push({
                event: "follow_target",
                at_ms: now - started,
                target: evidence.position,
                target_airborne: evidence.target_airborne
              });
              reachabilityTracker.markFollowUpdate();
            }
            if (reachabilityTracker.shouldDeclareMeleeUnreachable(now)) {
              const killDelta = combatTracker.killCount(entityName) - initialKillCount;
              return combatUnreachableFailure(
                bot,
                action.type,
                entityName,
                mode,
                reachabilityTracker.snapshot(now),
                {
                  attacks,
                  shots,
                  weapon: weaponResult.weapon,
                  equipment: weaponResult.equipment,
                  kill_stat_delta: killDelta,
                  confirmed_kill_delta: killDelta,
                  kill_count_source: "mineflayer_entity_dead",
                  kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
                  combat_events: boundedCombatEvents(events),
                  target: evidence
                }
              );
            }
          } else if (now - lastAttackAt >= COMBAT_ATTACK_INTERVAL_MS) {
            bot.pathfinder.setGoal(null);
            await bot.lookAt(lastTarget.position.offset(0, 1, 0), true);
            combatTracker.markAttack(lastTarget, "melee");
            reachabilityTracker.markAttack(now);
            bot.attack(lastTarget);
            attacks += 1;
            lastAttackAt = now;
            events.push({ event: "melee_attack", at_ms: now - started, distance: evidence.distance });
          }
        } else {
          if (ammoName && !findInventoryItem(bot, ammoName)) {
            return combatFailure(bot, action.type, entityName, mode, "no_ammo", `Inventory no longer contains ammo ${ammoName}.`, {
              attacks,
              shots,
              weapon: weaponResult.weapon,
              equipment: weaponResult.equipment,
              target: evidence,
              suggested_next_actions: ["query_inventory"]
            });
          }
          if (!evidence.line_of_sight) {
            return combatFailure(bot, action.type, entityName, mode, "no_line_of_sight", "Ranged engagement requires line of sight to the target.", {
              attacks,
              shots,
              weapon: weaponResult.weapon,
              equipment: weaponResult.equipment,
              target: evidence,
              suggested_next_actions: ["move_to", "scan_entities"]
            });
          }
          if (Number(evidence.distance) > maxDistance) {
            if (now - lastPathUpdate >= COMBAT_TARGET_REFRESH_MS) {
              bot.pathfinder.setGoal(new goals.GoalNear(lastTarget.position.x, lastTarget.position.y, lastTarget.position.z, COMBAT_RANGED_PREFERRED_DISTANCE), true);
              lastPathUpdate = now;
              events.push({ event: "path_to_ranged_distance", at_ms: now - started, target: evidence.position });
            }
          } else if (now - lastAttackAt >= COMBAT_RANGED_DRAW_MS + 250) {
            bot.pathfinder.setGoal(null);
            await bot.lookAt(lastTarget.position.offset(0, 1, 0), true);
            combatTracker.markAttack(lastTarget, "ranged");
            bot.activateItem();
            await wait(numberArg(action.args.draw_ms, COMBAT_RANGED_DRAW_MS));
            deactivateItem(bot);
            shots += 1;
            lastAttackAt = Date.now();
            events.push({ event: "ranged_shot", at_ms: lastAttackAt - started, distance: evidence.distance });
          }
        }

        await wait(COMBAT_TICK_MS);
      }
    } finally {
      bot.pathfinder.setGoal(null);
      clearMovement(bot);
    }

    const finalKillDelta = combatTracker.killCount(entityName) - initialKillCount;
    if (finalKillDelta > 0) {
      return combatSuccess(bot, action.type, entityName, mode, "target_killed", started, {
        attacks,
        shots,
        weapon: weaponResult.weapon,
        equipment: weaponResult.equipment,
        kill_stat_delta: finalKillDelta,
        confirmed_kill_delta: finalKillDelta,
        kill_count_source: "mineflayer_entity_dead",
        kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
        combat_events: boundedCombatEvents(events),
        target: lastTarget ? entityCombatEvidence(bot, lastTarget) : null
      });
    }
    const finalTargetEvidence = lastTarget ? entityCombatEvidence(bot, lastTarget) : null;
    if (mode === "melee" && finalTargetEvidence && !finalTargetEvidence.melee_reachable) {
      return combatUnreachableFailure(
        bot,
        action.type,
        entityName,
        mode,
        reachabilityTracker.snapshot(Date.now()),
        {
          attacks,
          shots,
          weapon: weaponResult.weapon,
          equipment: weaponResult.equipment,
          kill_stat_delta: finalKillDelta,
          confirmed_kill_delta: finalKillDelta,
          kill_count_source: "mineflayer_entity_dead",
          kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
          combat_events: boundedCombatEvents(events),
          target: finalTargetEvidence
        }
      );
    }
    return combatFailure(bot, action.type, entityName, mode, "timeout", `Combat engagement timed out after ${maxDurationMs}ms.`, {
      attacks,
      shots,
      weapon: weaponResult.weapon,
      equipment: weaponResult.equipment,
      kill_stat_delta: finalKillDelta,
      confirmed_kill_delta: finalKillDelta,
      kill_count_source: "mineflayer_entity_dead",
      kill_evidence: combatTracker.eventsSince(killEventCursor, entityName),
      combat_events: boundedCombatEvents(events),
      target: lastTarget ? entityCombatEvidence(bot, lastTarget) : null,
      suggested_next_actions: ["scan_entities", "consume_item", "equip_item", "move_to_and_engage_combat"]
    });
  });
}

/** Run one action body with a wall-clock timeout and normalized failure response. */
async function withTimeout(
  bot: Bot,
  actionType: string,
  timeoutMs: number,
  body: () => Promise<ActionResult>,
  timeoutDetails?: () => Record<string, unknown>,
  onTimeout?: () => void
): Promise<ActionResult> {
  let timeout: NodeJS.Timeout | null = null;
  try {
    return await Promise.race([
      body(),
      new Promise<ActionResult>((resolve) => {
        timeout = setTimeout(() => {
          clearMovement(bot);
          onTimeout?.();
          resolve(
            failure(bot, actionType, "timeout", `Action timed out after ${timeoutMs}ms.`, true, {
              timeout_ms: timeoutMs,
              ...(timeoutDetails?.() ?? {})
            })
          );
        }, timeoutMs);
      })
    ]);
  } catch (error) {
    return failure(bot, actionType, "runtime_error", errorToString(error), true);
  } finally {
    if (timeout) {
      clearTimeout(timeout);
    }
  }
}

/** Create a movement config that delegates terrain navigation details to pathfinder. */
function navigationMovements(bot: Bot): InstanceType<typeof Movements> {
  const movements = new Movements(bot);
  movements.canDig = MOVEMENT_POLICY.can_dig;
  movements.allow1by1towers = MOVEMENT_POLICY.allow_1x1_towers;
  movements.scafoldingBlocks = scaffoldingItemIds(bot, movements.scafoldingBlocks);
  movements.allowParkour = MOVEMENT_POLICY.allow_parkour;
  movements.allowSprinting = MOVEMENT_POLICY.allow_sprinting;
  movements.maxDropDown = MOVEMENT_POLICY.max_drop_down;
  movements.allowFreeMotion = true;
  return movements;
}

/** Return item ids that pathfinder may spend for local climbing or bridging. */
function scaffoldingItemIds(bot: Bot, defaults: number[]): number[] {
  const itemIds = new Set(defaults);
  for (const item of bot.inventory.items()) {
    if (!SAFE_SCAFFOLDING_ITEM_NAMES.has(item.name)) {
      continue;
    }
    const block = bot.registry.blocksByName[item.name];
    if (!block || block.boundingBox !== "block") {
      continue;
    }
    itemIds.add(item.type);
  }
  return [...itemIds];
}

/** Convert scaffold item ids to stable Minecraft item names for audit. */
function scaffoldingNames(bot: Bot, itemIds: number[]): string[] {
  return itemIds
    .map((itemId) => bot.registry.items[itemId]?.name)
    .filter((name): name is string => typeof name === "string")
    .sort();
}

/** Convert pathfinder failures into a model-readable action result. */
function navigationFailure(
  bot: Bot,
  actionType: string,
  target: Vec3,
  tolerance: number,
  timeoutMs: number,
  errorCode: string,
  diagnosis: string,
  path: ComputedPath | PartiallyComputedPath | null,
  pathResets: string[],
  startPosition: Vec3,
  initialDistance: number,
  message?: string,
  scaffoldingItemNames: string[] = [],
  availableScaffoldingCount = 0,
  planningTimeoutMs = 0,
  inventoryBefore: Record<string, number> = {}
): ActionResult {
  const nearestReachablePosition = pathLastNode(path);
  const heightDelta = target.y - bot.entity.position.y;
  const endPosition = bot.entity.position.clone();
  const finalDistance = endPosition.distanceTo(target);
  const progressStatus = navigationProgressStatus(errorCode, initialDistance, finalDistance, tolerance);
  const pathDiagnostics = pathSummary(path);
  const inventoryAfter = inventoryCounts(bot);
  const inventoryDelta = inventoryNetDeltaCounts(inventoryBefore, inventoryAfter);
  const scaffoldingDelta = filteredDeltaCounts(inventoryDelta, scaffoldingItemNames);
  const failureReason = navigationFailureReason(errorCode, pathResets, pathDiagnostics, availableScaffoldingCount);
  const recoveryText = nearestReachablePosition
    ? ` The nearest reachable path node was ${formatVec3(nearestReachablePosition)}; try that reachable ground coordinate, scan again, or alter terrain explicitly.`
    : " No reachable intermediate path node was found; scan nearby terrain or choose another target.";
  return failure(bot, actionType, errorCode, message ?? diagnosis, true, {
    target: vec3ToJson(target),
    target_position: vec3ToJson(target),
    start_position: vec3ToJson(startPosition),
    end_position: vec3ToJson(endPosition),
    tolerance,
    timeout_ms: timeoutMs,
    planning_timeout_ms: planningTimeoutMs,
    initial_distance: initialDistance,
    final_distance: finalDistance,
    distance_delta: initialDistance - finalDistance,
    reached_tolerance: finalDistance <= tolerance,
    progress_status: progressStatus,
    diagnosis,
    navigation_failure_reason: failureReason,
    state_summary: `${diagnosis}${recoveryText} ${failureReason} Pathfinder may dig reachable blocking blocks and use safe scaffold blocks automatically; if it still fails, scan nearby blocks, gather safe scaffold blocks, dig or place an explicit block, scan dropped items if the target moved, or choose another target.`,
    movement_policy: MOVEMENT_POLICY,
    scaffolding_item_names: scaffoldingItemNames,
    available_scaffolding_count: availableScaffoldingCount,
    inventory_before: inventoryBefore,
    inventory_after: inventoryAfter,
    inventory_delta: inventoryDelta,
    consumed_items: consumedItemCounts(inventoryDelta),
    scaffolding_delta: scaffoldingDelta,
    scaffolding_consumed: consumedItemCounts(scaffoldingDelta),
    suggested_affordances: NAVIGATION_AFFORDANCES,
    nearest_reachable_position: nearestReachablePosition ? vec3ToJson(nearestReachablePosition) : null,
    target_height_delta: heightDelta,
    path_summary: pathDiagnostics,
    requires_break_count: pathDiagnostics?.requires_break_count ?? 0,
    requires_place_count: pathDiagnostics?.requires_place_count ?? 0,
    has_parkour: pathDiagnostics?.has_parkour ?? false,
    path_resets: pathResets
  });
}

/** Summarize pathfinder path data without exposing plugin internals. */
function pathSummary(path: ComputedPath | PartiallyComputedPath | null): Record<string, unknown> | null {
  if (!path) {
    return null;
  }
  const last = pathLastNode(path);
  return {
    status: path.status,
    path_length: path.path.length,
    cost: path.cost,
    time_ms: path.time,
    visited_nodes: path.visitedNodes,
    generated_nodes: path.generatedNodes,
    last_node: last ? vec3ToJson(last) : null,
    requires_break_count: path.path.reduce((total, move) => total + move.toBreak.length, 0),
    requires_place_count: path.path.reduce((total, move) => total + move.toPlace.length, 0),
    has_parkour: path.path.some((move) => move.parkour)
  };
}

/** Explain likely navigation blockers in terms the agent can act on next turn. */
function navigationFailureReason(
  errorCode: string,
  pathResets: string[],
  pathDiagnostics: Record<string, unknown> | null,
  availableScaffoldingCount: number
): string {
  const requiredPlaceCount = numberRecordValue(pathDiagnostics, "requires_place_count") ?? 0;
  const requiredBreakCount = numberRecordValue(pathDiagnostics, "requires_break_count") ?? 0;
  const safeMaterials = "dirt, cobblestone, stone, deepslate, netherrack, sand, or gravel";
  if (pathResets.includes("no_scaffolding_blocks")) {
    return `Pathfinder needed scaffold blocks but available safe scaffold count is ${availableScaffoldingCount}. Check inventory; if there are not enough safe blocks, gather expendable blocks such as ${safeMaterials} before retrying move_to.`;
  }
  if (requiredPlaceCount > availableScaffoldingCount) {
    return `The planned route requires about ${requiredPlaceCount} scaffold placement(s), but only ${availableScaffoldingCount} safe scaffold block(s) are available. Gather expendable blocks such as ${safeMaterials}, or choose a lower/easier route.`;
  }
  if (pathResets.includes("place_error")) {
    return `Pathfinder tried to place support but placement failed. Check whether inventory contains safe scaffold blocks (${safeMaterials}) and whether the target area has a valid support face.`;
  }
  if (pathResets.includes("dig_error")) {
    return "Pathfinder tried to dig a blocking block but digging failed. Scan nearby blocks and consider an explicit dig_block_at on a supported coordinate or choose another route.";
  }
  if (pathResets.includes("stuck")) {
    return "Pathfinder reported stuck movement. Re-scan the area, choose a nearby reachable intermediate coordinate, or alter terrain explicitly.";
  }
  if (["no_path", "path_timeout", "timeout"].includes(errorCode) && availableScaffoldingCount === 0) {
    return `No safe scaffold blocks are currently available for automatic placement. If the route has a height gap, cliff, or missing support, gather expendable blocks such as ${safeMaterials} before retrying move_to.`;
  }
  if (requiredBreakCount > 0) {
    return `The route may require breaking about ${requiredBreakCount} blocking block(s). If automatic digging keeps failing, scan and dig a specific blocker explicitly.`;
  }
  return "No scaffold shortage was directly reported; use path_summary, path_resets, and nearest_reachable_position to choose the next navigation attempt.";
}

/** Read one numeric field from a path diagnostic record. */
function numberRecordValue(record: Record<string, unknown> | null, key: string): number | null {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Choose a movement timeout from distance unless the agent supplied a bounded value. */
function movementTimeoutMs(value: unknown, initialDistance: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return clamp(Math.floor(value), MOVE_TIMEOUT_MIN_MS, MOVE_TIMEOUT_EXPLICIT_MAX_MS);
  }
  const estimated = MOVE_TIMEOUT_MIN_MS + initialDistance * MOVE_TIMEOUT_PER_BLOCK_MS;
  return clamp(Math.floor(estimated), MOVE_TIMEOUT_MIN_MS, MOVE_TIMEOUT_DEFAULT_MAX_MS);
}

/** Give pathfinder enough planning budget for terrain-editing routes without consuming the whole action. */
function movementPlanningTimeoutMs(timeoutMs: number): number {
  return clamp(Math.floor(timeoutMs * 0.5), 3000, 15000);
}

/** Classify whether a failed navigation action still moved closer to the target. */
function navigationProgressStatus(
  errorCode: string,
  initialDistance: number,
  finalDistance: number,
  tolerance: number
): string {
  if (finalDistance <= tolerance) {
    return "reached";
  }
  if (errorCode === "no_path") {
    return "no_path";
  }
  if (errorCode === "path_timeout") {
    return "path_timeout";
  }
  if (errorCode === "path_stopped") {
    return "path_stopped";
  }
  if (initialDistance - finalDistance > Math.max(1, tolerance)) {
    return "partial_progress";
  }
  return "timeout_no_progress";
}

/** Clamp a numeric value to a closed range. */
function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

/** Return the last path node as the best known reachable coordinate. */
function pathLastNode(path: ComputedPath | PartiallyComputedPath | null): Vec3 | null {
  if (!path || path.path.length === 0) {
    return null;
  }
  const last = path.path[path.path.length - 1];
  return new Vec3(last.x, last.y, last.z);
}

/** Render a Vec3 in the same compact shape used in model-facing summaries. */
function formatVec3(position: Vec3): string {
  return `(${position.x},${position.y},${position.z})`;
}

/** Bound pathfinder.goto with a wall-clock movement timeout. */
async function withNavigationTimeout(bot: Bot, promise: Promise<void>, timeoutMs: number): Promise<void> {
  let timeout: NodeJS.Timeout | null = null;
  try {
    await Promise.race([
      promise,
      new Promise<void>((_resolve, reject) => {
        timeout = setTimeout(() => {
          const error = new Error(`Timed out moving to target within ${timeoutMs}ms.`);
          error.name = "NavigationTimeout";
          bot.pathfinder.setGoal(null);
          reject(error);
        }, timeoutMs);
      })
    ]);
  } finally {
    if (timeout) {
      clearTimeout(timeout);
    }
  }
}

/** Normalize pathfinder error names into stable audit codes. */
function navigationErrorCode(errorName: string): string {
  if (errorName === "NoPath") {
    return "no_path";
  }
  if (errorName === "Timeout") {
    return "path_timeout";
  }
  if (errorName === "NavigationTimeout") {
    return "timeout";
  }
  if (errorName === "PathStopped") {
    return "path_stopped";
  }
  return "navigation_error";
}

/** Describe what failed while keeping strategy decisions with the model. */
function navigationDiagnosis(errorCode: string): string {
  if (errorCode === "no_path") {
    return "Mineflayer pathfinder could not find a route under the current dig/place movement policy.";
  }
  if (errorCode === "path_timeout") {
    return "Mineflayer pathfinder could not finish computing a route to the target before the planning timeout.";
  }
  if (errorCode === "timeout") {
    return "The worker found a path or started moving, but did not reach the target before the movement timeout.";
  }
  if (errorCode === "path_stopped") {
    return "Pathfinding stopped before the target was reached.";
  }
  return "Navigation failed before the target was reached.";
}

/** Normalize model-facing station aliases into worker processing modes. */
function normalizeProcessStation(station: string | null): "inventory" | "crafting_table" | "nearby_crafting_table" | "furnace" {
  switch ((station ?? "inventory").toLowerCase()) {
    case "furnace":
    case "smelting":
    case "smelt":
      return "furnace";
    case "crafting_table":
    case "nearby_crafting_table":
    case "workbench":
    case "3x3":
      return "crafting_table";
    case "inventory":
    case "hand":
    case "2x2":
    default:
      return "inventory";
  }
}

/** Find the nearest block by canonical Mineflayer block name. */
function findNearestBlock(bot: Bot, name: string, maxDistance: number): Block | null {
  const positions = bot.findBlocks({
    matching: (block) => block.name === name,
    maxDistance,
    count: 1
  });
  const position = positions[0];
  return position ? bot.blockAt(position) : null;
}

/** Infer a furnace input for common Minecraft smelting outputs. */
function smeltingInputForOutput(outputName: string | null): string | null {
  return outputName ? FURNACE_INPUT_BY_OUTPUT.get(outputName) ?? null : null;
}

/** Choose an available furnace fuel without hiding resource acquisition from the agent. */
function firstAvailableFuelName(bot: Bot): string | null {
  for (const fuelName of FURNACE_FUEL_ITEM_NAMES) {
    if (findInventoryItem(bot, fuelName)) {
      return fuelName;
    }
  }
  return null;
}

/** Wait for furnace output and take it into inventory once the expected item appears. */
async function takeFurnaceOutput(
  furnace: RuntimeFurnace,
  outputName: string,
  desiredCount: number,
  timeoutMs: number
): Promise<number> {
  const started = Date.now();
  let collected = 0;
  while (Date.now() - started < timeoutMs && collected < desiredCount) {
    const output = readFurnaceOutput(furnace);
    if (output && output.name === outputName && Number(output.count ?? 0) > 0) {
      const taken = await furnace.takeOutput();
      collected += Math.max(1, Number(taken?.count ?? output.count ?? 1));
      continue;
    }
    await wait(500);
  }
  return collected;
}

/** Read current furnace output across Mineflayer versions. */
function readFurnaceOutput(furnace: RuntimeFurnace): { name?: string; count?: number } | null {
  if (typeof furnace.outputItem === "function") {
    return furnace.outputItem();
  }
  return furnace.outputItem ?? null;
}

/** Identify dropped item entities across Minecraft versions. */
function isDroppedItemEntity(entity: RuntimeEntity): boolean {
  if (entity.name === "item" || entity.objectType === "Item") {
    return true;
  }
  if (typeof entity.getDroppedItem !== "function") {
    return false;
  }
  return entity.getDroppedItem() !== null;
}

/** Snapshot dropped-item ids already present before one causally bounded world action. */
function droppedItemEntityIds(bot: Bot): Set<number> {
  return new Set(
    (Object.values(bot.entities) as RuntimeEntity[])
      .filter((entity) => isDroppedItemEntity(entity))
      .map((entity) => entity.id)
  );
}

/** Return newly server-observed item entities near one changed block position. */
function newlyObservedDroppedItems(
  bot: Bot,
  idsBefore: Set<number>,
  origin: Vec3,
  maxDistance: number
): Record<string, unknown>[] {
  return (Object.values(bot.entities) as RuntimeEntity[])
    .filter((entity) => !idsBefore.has(entity.id))
    .filter((entity) => isDroppedItemEntity(entity))
    .filter((entity) => entity.position.distanceTo(origin.offset(0.5, 0.5, 0.5)) <= maxDistance)
    .map((entity) => {
      const dropped = entity.getDroppedItem?.();
      return {
        entity_id: entity.id,
        item: dropped?.name ?? null,
        count: dropped?.count ?? null,
        position: vec3ToJson(entity.position),
        distance_from_block: entity.position.distanceTo(origin.offset(0.5, 0.5, 0.5))
      };
    })
    .sort((left, right) => Number(left.distance_from_block) - Number(right.distance_from_block));
}

/** Return item entities newly observed near one entity interaction position. */
function newlyObservedDroppedItemsNearEntity(
  bot: Bot,
  idsBefore: Set<number>,
  origin: Vec3,
  maxDistance: number
): Record<string, unknown>[] {
  return (Object.values(bot.entities) as RuntimeEntity[])
    .filter((entity) => !idsBefore.has(entity.id))
    .filter((entity) => isDroppedItemEntity(entity))
    .filter((entity) => entity.position.distanceTo(origin) <= maxDistance)
    .map((entity) => {
      const dropped = entity.getDroppedItem?.();
      return {
        entity_id: entity.id,
        item: dropped?.name ?? null,
        count: dropped?.count ?? null,
        position: vec3ToJson(entity.position),
        distance_from_target: entity.position.distanceTo(origin)
      };
    })
    .sort((left, right) => Number(left.distance_from_target) - Number(right.distance_from_target));
}

/** Find a nearby entity by id, display name, Mineflayer name, or type. */
function findNearestEntity(bot: Bot, name: string, maxDistance: number): RuntimeEntity | null {
  return (
    (Object.values(bot.entities) as RuntimeEntity[])
      .filter((entity) => entity.id !== bot.entity.id)
      .filter((entity) => bot.entity.position.distanceTo(entity.position) <= maxDistance)
      .filter((entity) => {
        const entityId = String(entity.id);
        const entityName = String(entity.name ?? "");
        const entityType = String(entity.type ?? "");
        const displayName = String(entity.displayName ?? "");
        return [entityId, entityName, entityType, displayName].includes(name);
      })
      .sort((left, right) => {
        return bot.entity.position.distanceTo(left.position) - bot.entity.position.distanceTo(right.position);
      })[0] ?? null
  );
}

/** Keep one engagement locked to the originally selected entity id. */
function findEntityById(bot: Bot, entityId: number, maxDistance: number): RuntimeEntity | null {
  const entity = bot.entities[entityId] as RuntimeEntity | undefined;
  if (!entity || entity.id === bot.entity.id || entity.isValid === false) {
    return null;
  }
  return bot.entity.position.distanceTo(entity.position) <= maxDistance ? entity : null;
}

/** Return whether an entity matches a canonical id, Mineflayer name, display name, or numeric id. */
function entityMatches(entity: RuntimeEntity, name: string): boolean {
  const entityId = String(entity.id);
  const entityName = String(entity.name ?? "");
  const entityType = String(entity.type ?? "");
  const displayName = String(entity.displayName ?? "");
  return [entityId, entityName, entityType, displayName].includes(name);
}

/** Build model-facing combat evidence for one entity. */
function entityCombatEvidence(bot: Bot, entity: RuntimeEntity): Record<string, unknown> {
  const distance = bot.entity.position.distanceTo(entity.position);
  const heightDelta = entity.position.y - bot.entity.position.y;
  const lineOfSight = canSeeEntity(bot, entity);
  const targetAirborne = isEntityAirborne(bot, entity);
  const meleeReachable = distance <= COMBAT_MELEE_RANGE && lineOfSight;
  const reachabilityStatus = meleeReachable
    ? "in_melee_range"
    : !lineOfSight
      ? "line_of_sight_blocked"
      : targetAirborne
        ? "temporarily_out_of_melee_range"
        : "approach_required";
  return {
    entity_id: entity.id,
    id: entity.id,
    name: entity.name ?? entity.type,
    type: String(entity.type),
    display_name: entity.displayName ?? null,
    position: vec3ToJson(entity.position),
    distance,
    height_delta: heightDelta,
    line_of_sight: lineOfSight,
    target_airborne: targetAirborne,
    airborne_is_terminal: false,
    melee_reachable: meleeReachable,
    reachability_status: reachabilityStatus,
    suggested_modes: meleeReachable ? ["melee"] : ["ranged", "melee"],
    details: entityServerDetails(bot, entity)
  };
}

/** Use Mineflayer visibility helpers when available, otherwise keep evidence permissive. */
function canSeeEntity(bot: Bot, entity: RuntimeEntity): boolean {
  const maybeBot = bot as unknown as { canSeeEntity?: (target: RuntimeEntity) => boolean };
  return maybeBot.canSeeEntity?.(entity) ?? true;
}

/** Validate the currently equipped combat item without changing equipment for the agent. */
function validateCombatEquipment(
  bot: Bot,
  mode: "melee" | "ranged",
  weaponName: string | null,
  ammoName: string | null
): Record<string, unknown> {
  const equipment = equipmentSnapshotForAction(bot);
  const currentWeapon = equipmentNameForDestination(bot, "hand") ?? bot.heldItem?.name ?? null;
  if (weaponName && currentWeapon !== weaponName) {
    return {
      ok: false,
      status: "weapon_not_equipped",
      message: `Expected ${weaponName} in main hand, but current main hand is ${currentWeapon ?? "empty"}. Use equip_item before move_to_and_engage_combat if that weapon is desired.`,
      current_weapon: currentWeapon,
      equipment,
      suggested_modes: mode === "melee" ? ["ranged"] : ["melee"],
      suggested_next_actions: ["equip_item", "query_inventory"]
    };
  }
  if (mode === "ranged" && (!currentWeapon || !RANGED_WEAPON_NAMES.has(currentWeapon))) {
    return {
      ok: false,
      status: "weapon_not_equipped",
      message: `Ranged engagement requires a bow, crossbow, or trident in main hand; current main hand is ${currentWeapon ?? "empty"}.`,
      current_weapon: currentWeapon,
      equipment,
      suggested_modes: ["melee"],
      suggested_next_actions: ["equip_item", "query_inventory"]
    };
  }
  if (mode === "ranged" && ammoName && !findInventoryItem(bot, ammoName)) {
    return {
      ok: false,
      status: "no_ammo",
      message: `Inventory does not contain ammo ${ammoName}.`,
      current_weapon: currentWeapon,
      equipment,
      suggested_modes: ["melee"],
      suggested_next_actions: ["query_inventory", "equip_item"]
    };
  }
  return { ok: true, weapon: currentWeapon ?? "barehand", equipment };
}

/** Build a successful combat result with bounded event evidence. */
function combatSuccess(
  bot: Bot,
  actionType: string,
  entity: string,
  mode: string,
  status: string,
  started: number,
  details: Record<string, unknown>
): ActionResult {
  return success(bot, actionType, {
    entity,
    mode,
    status,
    duration_ms: Date.now() - started,
    final_health: bot.health,
    final_food: bot.food,
    state_summary: `Combat ${status} for ${entity} using ${mode} mode.`,
    ...details
  });
}

/** Build a recoverable combat result that gives control back to the agent. */
function combatFailure(
  bot: Bot,
  actionType: string,
  entity: string,
  mode: string,
  status: string,
  message: string,
  details: Record<string, unknown> = {}
): ActionResult {
  return failure(bot, actionType, status, message, true, {
    entity,
    mode,
    status,
    final_health: bot.health,
    final_food: bot.food,
    state_summary: `${message} The agent should decide whether to scan, switch combat mode, consume an item, reposition, or re-engage.`,
    ...details
  });
}

/** Return bounded evidence after dynamic melee tracking cannot enter attack range. */
function combatUnreachableFailure(
  bot: Bot,
  actionType: string,
  entity: string,
  mode: string,
  diagnostics: CombatReachabilitySnapshot,
  details: Record<string, unknown>
): ActionResult {
  const closestDistance = diagnostics.closest_distance;
  const targetAirborne = diagnostics.target_airborne === true;
  const distanceText = closestDistance === null ? "unknown" : closestDistance.toFixed(2);
  const diagnosis = targetAirborne
    ? "The target remained outside melee range while airborne or separated by height during this engagement; it may become reachable after landing."
    : "Dynamic pathfinder following stopped making enough distance progress to enter melee range during this engagement. Inspect current terrain and target position before retrying.";
  return combatFailure(
    bot,
    actionType,
    entity,
    mode,
    "target_unreachable",
    `Dynamic tracking could not bring ${entity} into melee range; closest distance was ${distanceText} blocks.`,
    {
      ...details,
      ...diagnostics,
      diagnosis,
      recovery_guidance: [
        "Use scan_entities again because a moving or airborne target may have changed position or become reachable.",
        "Use scan_blocks or move_to when terrain or height appears to block the current approach.",
        "If suitable ranged gear exists, use equip_item explicitly, then call move_to_and_engage_combat with mode=ranged."
      ],
      suggested_modes: targetAirborne ? ["ranged", "melee"] : ["melee", "ranged"],
      suggested_next_actions: [
        "scan_entities",
        "scan_blocks",
        "move_to",
        "query_inventory",
        "equip_item",
        "move_to_and_engage_combat"
      ]
    }
  );
}

/** Keep combat audit evidence bounded so action results stay prompt-safe. */
function boundedCombatEvents(events: Record<string, unknown>[]): Record<string, unknown>[] {
  if (events.length <= 12) {
    return events;
  }
  return [...events.slice(0, 6), { event: "events_omitted", count: events.length - 12 }, ...events.slice(-6)];
}

/** Release a drawn ranged item when the Mineflayer version exposes the helper. */
function deactivateItem(bot: Bot): void {
  const maybeBot = bot as unknown as { deactivateItem?: () => void };
  maybeBot.deactivateItem?.();
}

/** Find an inventory item by canonical Mineflayer item name. */
function findInventoryItem(bot: Bot, name: string) {
  return bot.inventory.items().find((item) => item.name === name) ?? null;
}

/** Return a compact action-result equipment snapshot. */
function equipmentSnapshotForAction(bot: Bot): Record<string, unknown> {
  return {
    main_hand: equipmentItemForDestination(bot, "hand"),
    off_hand: equipmentItemForDestination(bot, "off-hand"),
    head: equipmentItemForDestination(bot, "head"),
    chest: equipmentItemForDestination(bot, "torso"),
    legs: equipmentItemForDestination(bot, "legs"),
    feet: equipmentItemForDestination(bot, "feet")
  };
}

/** Read one equipped item from Mineflayer's equipment destination slots. */
function equipmentItemForDestination(bot: Bot, destination: string): Record<string, unknown> | null {
  try {
    const maybeBot = bot as unknown as { getEquipmentDestSlot?: (destination: string) => number };
    const slot = maybeBot.getEquipmentDestSlot?.(destination);
    if (typeof slot !== "number") {
      return null;
    }
    const item = bot.inventory.slots[slot];
    return item ? { name: item.name, count: item.count } : null;
  } catch {
    return null;
  }
}

/** Return only the equipped item name for combat validation. */
function equipmentNameForDestination(bot: Bot, destination: string): string | null {
  const item = equipmentItemForDestination(bot, destination);
  const name = item?.name;
  return typeof name === "string" ? name : null;
}

/** Normalize model-facing slot aliases to Mineflayer equipment destinations. */
function normalizeEquipmentDestination(slot: string | null): string {
  switch ((slot ?? "hand").toLowerCase()) {
    case "hand":
    case "main_hand":
    case "mainhand":
      return "hand";
    case "off_hand":
    case "off-hand":
    case "offhand":
    case "shield":
      return "off-hand";
    case "head":
    case "helmet":
      return "head";
    case "chest":
    case "torso":
    case "chestplate":
      return "torso";
    case "legs":
    case "leggings":
      return "legs";
    case "feet":
    case "boots":
      return "feet";
    default:
      return slot ?? "hand";
  }
}

/** Convert Mineflayer equipment destinations back to stable prompt-facing slot names. */
function publicEquipmentSlot(destination: string): string {
  if (destination === "off-hand") {
    return "off_hand";
  }
  if (destination === "torso") {
    return "chest";
  }
  return destination;
}

/** Find a support block adjacent to an explicit target placement coordinate. */
function findPlacementAgainstTarget(bot: Bot, target: Vec3): BlockPlacement | null {
  const targetBlock = bot.blockAt(target);
  if ((targetBlock && targetBlock.name !== "air") || isPlacementBlockedByEntity(bot, target)) {
    return null;
  }

  for (const direction of ADJACENT_DIRECTIONS) {
    const referencePosition = target.minus(direction);
    const reference = bot.blockAt(referencePosition);
    if (reference && reference.name !== "air") {
      return { reference, face: direction, target };
    }
  }
  return null;
}

/** Find a nearby air block with support for default block placement. */
function findNearbyPlacement(bot: Bot, radius: number): BlockPlacement | null {
  return findNearbyPlacements(bot, radius, 1)[0] ?? null;
}

/** Find nearby supported placement targets sorted by distance from the bot. */
function findNearbyPlacements(bot: Bot, radius: number, limit: number): BlockPlacement[] {
  const origin = bot.entity.position.floored();
  const candidates: Vec3[] = [];
  for (let dx = -radius; dx <= radius; dx += 1) {
    for (let dz = -radius; dz <= radius; dz += 1) {
      for (const dy of [-1, 0, 1]) {
        candidates.push(origin.offset(dx, dy, dz));
      }
    }
  }

  candidates.sort((left, right) => bot.entity.position.distanceTo(left) - bot.entity.position.distanceTo(right));
  const placements: BlockPlacement[] = [];
  const seenTargets = new Set<string>();
  for (const target of candidates) {
    const placement = findPlacementAgainstTarget(bot, target);
    if (!placement) {
      continue;
    }
    const key = `${placement.target.x},${placement.target.y},${placement.target.z}`;
    if (seenTargets.has(key)) {
      continue;
    }
    seenTargets.add(key);
    placements.push(placement);
    if (placements.length >= limit) {
      break;
    }
  }
  return placements;
}

/** Find a simple placement target on top of the block underneath the bot. */
function findPlacementBelowBot(bot: Bot): BlockPlacement | null {
  const base = new Vec3(
    Math.floor(bot.entity.position.x),
    Math.floor(bot.entity.position.y) - 1,
    Math.floor(bot.entity.position.z)
  );
  const reference = bot.blockAt(base);
  if (!reference || reference.name === "air" || isPlacementBlockedByEntity(bot, base.offset(0, 1, 0))) {
    return null;
  }
  return { reference, face: new Vec3(0, 1, 0), target: base.offset(0, 1, 0) };
}

/** Convert a placement candidate into compact model-facing diagnostics. */
function placementToJson(placement: BlockPlacement): Record<string, unknown> {
  return {
    target: vec3ToJson(placement.target),
    reference: vec3ToJson(placement.reference.position),
    reference_block: placement.reference.name,
    face: vec3ToJson(placement.face)
  };
}

/** Avoid placing blocks into the bot, players, mobs, or other blocking entity positions. */
function isPlacementBlockedByEntity(bot: Bot, target: Vec3): boolean {
  const center = target.offset(0.5, 0, 0.5);
  return (Object.values(bot.entities) as RuntimeEntity[]).some((entity) => {
    if (entity.type === "object" || entity.type === "orb" || entity.type === "projectile") {
      return false;
    }
    const horizontalDistance = Math.hypot(entity.position.x - center.x, entity.position.z - center.z);
    const verticalDistance = Math.abs(entity.position.y - target.y);
    return horizontalDistance < 0.9 && verticalDistance < 1.8;
  });
}

/** Build a successful action result with a fresh observation. */
function success(bot: Bot, actionType: string, details: Record<string, unknown> = {}): ActionResult {
  return {
    ok: true,
    action_type: actionType,
    ...details,
    observation: observe(bot)
  };
}

/** Build a normalized failure result with recoverability metadata. */
function failure(
  bot: Bot,
  actionType: string,
  errorCode: string,
  message: string,
  recoverable: boolean,
  details: Record<string, unknown> = {}
): ActionResult {
  return {
    ok: false,
    action_type: actionType,
    error_code: errorCode,
    message,
    recoverable,
    ...details,
    observation: observe(bot)
  };
}

/** Return a compact JSON inventory snapshot. */
function inventorySnapshot(bot: Bot) {
  return bot.inventory.items().map((item) => ({
    name: item.name,
    count: item.count
  }));
}

/** Return inventory counts by item name for action-level delta reporting. */
function inventoryCounts(bot: Bot): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const item of bot.inventory.items()) {
    counts[item.name] = (counts[item.name] ?? 0) + item.count;
  }
  return counts;
}

/** Compute positive inventory deltas after an action. */
function inventoryDeltaCounts(before: Record<string, number>, after: Record<string, number>): Record<string, number> {
  const delta: Record<string, number> = {};
  for (const [name, count] of Object.entries(after)) {
    const added = count - (before[name] ?? 0);
    if (added > 0) {
      delta[name] = added;
    }
  }
  return delta;
}

/** Compute signed inventory deltas after an action. */
function inventoryNetDeltaCounts(before: Record<string, number>, after: Record<string, number>): Record<string, number> {
  const delta: Record<string, number> = {};
  const names = new Set([...Object.keys(before), ...Object.keys(after)]);
  for (const name of names) {
    const change = (after[name] ?? 0) - (before[name] ?? 0);
    if (change !== 0) {
      delta[name] = change;
    }
  }
  return delta;
}

/** Keep only delta entries whose item names are in the allow-list. */
function filteredDeltaCounts(delta: Record<string, number>, itemNames: string[]): Record<string, number> {
  const allowed = new Set(itemNames);
  const filtered: Record<string, number> = {};
  for (const [name, count] of Object.entries(delta)) {
    if (allowed.has(name)) {
      filtered[name] = count;
    }
  }
  return filtered;
}

/** Convert negative inventory deltas into positive consumed counts. */
function consumedItemCounts(delta: Record<string, number>): Record<string, number> {
  const consumed: Record<string, number> = {};
  for (const [name, count] of Object.entries(delta)) {
    if (count < 0) {
      consumed[name] = Math.abs(count);
    }
  }
  return consumed;
}

/** Stop all movement controls that a high-level action may have toggled. */
function clearMovement(bot: Bot): void {
  bot.clearControlStates();
}

/** Read a string action argument when provided. */
function stringArg(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Read a numeric action argument with a fallback. */
function numberArg(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** Read a numeric entity id from scan output or a numeric model string. */
function entityIdArg(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value) && value >= 0) {
    return value;
  }
  if (typeof value === "string" && /^\d+$/.test(value)) {
    return Number(value);
  }
  return null;
}

/** Read a boolean action argument with a fallback. */
function booleanArg(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/** Read a vector from a nested position-like object. */
function vectorArg(value: unknown): Vec3 | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  return vectorFromArgs(value as Record<string, unknown>);
}

/** Read a vector from x/y/z fields. */
function vectorFromArgs(args: Record<string, unknown>): Vec3 | null {
  const x = args.x;
  const y = args.y;
  const z = args.z;
  if (typeof x !== "number" || typeof y !== "number" || typeof z !== "number") {
    return null;
  }
  return new Vec3(x, y, z);
}

/** Convert Mineflayer Vec3 instances into JSON-safe coordinates. */
function vec3ToJson(position: Vec3) {
  return {
    x: position.x,
    y: position.y,
    z: position.z
  };
}

/** Sleep without depending on Mineflayer tick helpers. */
function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Normalize thrown values into response-safe strings. */
function errorToString(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
