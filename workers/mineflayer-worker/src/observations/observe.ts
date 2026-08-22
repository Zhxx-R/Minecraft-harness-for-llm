import type { Bot } from "mineflayer";
import type { Vec3 } from "vec3";
import { combatTrackerFor } from "../runtime/combat-tracker.js";
import { isEntityAirborne } from "../runtime/entity-state.js";
import { entityServerDetails } from "../runtime/entity-details.js";
import { persistentFollowSnapshot } from "../runtime/persistent-follow.js";
import { spawnSequence } from "../runtime/spawn-sequence.js";

/** Conservative hostile set aligned with backend threat-pause policy. */
const HOSTILE_ENTITY_NAMES = new Set([
  "blaze",
  "cave_spider",
  "creeper",
  "drowned",
  "elder_guardian",
  "ender_dragon",
  "endermite",
  "evoker",
  "ghast",
  "guardian",
  "hoglin",
  "husk",
  "magma_cube",
  "phantom",
  "piglin_brute",
  "pillager",
  "ravager",
  "shulker",
  "silverfish",
  "skeleton",
  "slime",
  "stray",
  "vex",
  "vindicator",
  "warden",
  "witch",
  "wither",
  "wither_skeleton",
  "zoglin",
  "zombie",
  "zombie_villager"
]);

/** Build the structured observation returned to the backend harness. */
export function observe(bot: Bot) {
  const stats = verifierStats(bot);
  const sortedEntities = Object.values(bot.entities)
    .filter((entity) => entity.id !== bot.entity.id)
    .sort((left, right) => {
      const leftDistance = bot.entity.position.distanceTo(left.position);
      const rightDistance = bot.entity.position.distanceTo(right.position);
      return leftDistance - rightDistance;
    });
  const nearestEntities = sortedEntities
    .slice(0, 10)
    .map((entity) => entityPayload(bot, entity));
  const nearbyHostileEntities = sortedEntities
    .filter((entity) => HOSTILE_ENTITY_NAMES.has(String(entity.name ?? "").toLowerCase()))
    .filter((entity) => bot.entity.position.distanceTo(entity.position) <= 64)
    .slice(0, 20)
    .map((entity) => entityPayload(bot, entity));

  const nearbyBlocks = bot.findBlocks({
    matching: () => true,
    maxDistance: 8,
    count: 32
  });

  return {
    entity_id: bot.entity.id,
    spawn_sequence: spawnSequence(bot),
    position: vec3ToJson(bot.entity.position),
    health: bot.health,
    food: bot.food,
    world: worldSnapshot(bot),
    stats,
    active_follow: persistentFollowSnapshot(bot),
    equipment: equipmentSnapshot(bot),
    inventory: bot.inventory.items().map((item) => ({
      name: item.name,
      count: item.count
    })),
    nearby_entities: nearestEntities,
    nearby_hostile_entities: nearbyHostileEntities,
    nearby_blocks: nearbyBlocks.map((position) => ({
      position: vec3ToJson(position),
      name: bot.blockAt(position)?.name ?? null
    }))
  };
}

/** Convert a Mineflayer entity into the compact observation payload. */
function entityPayload(bot: Bot, entity: Bot["entity"]) {
  const droppedItem = entity.getDroppedItem?.();
  return {
    entity_id: entity.id,
    id: entity.id,
    name: entity.name ?? entity.type,
    type: String(entity.type),
    dropped_item: droppedItem
      ? {
          name: droppedItem.name,
          count: droppedItem.count
        }
      : null,
    position: vec3ToJson(entity.position),
    distance: bot.entity.position.distanceTo(entity.position),
    height_delta: entity.position.y - bot.entity.position.y,
    line_of_sight: canSeeEntity(bot, entity),
    target_airborne: isEntityAirborne(bot, entity),
    details: entityServerDetails(bot, entity)
  };
}

/** Merge worker-confirmed death events with any native statistics exposed by Mineflayer. */
function verifierStats(bot: Bot): Record<string, unknown> {
  const nativeStats = normalizeStatistics((bot as unknown as { statistics?: unknown }).statistics);
  const combatStats = combatTrackerFor(bot).snapshot();
  return {
    ...nativeStats,
    native_kill_entity: nativeStats.kill_entity,
    kill_entity: combatStats.kill_entity,
    confirmed_kill_entity: combatStats.kill_entity,
    confirmed_kill_events: combatStats.confirmed_kill_events,
    kill_count_source: combatStats.source
  };
}

/** Convert Mineflayer Vec3 instances into JSON-safe coordinates. */
function vec3ToJson(position: Vec3) {
  return {
    x: position.x,
    y: position.y,
    z: position.z
  };
}

/** Return compact world time metadata used by long-run and survival verifiers. */
function worldSnapshot(bot: Bot) {
  return {
    age_ticks: bot.time?.age ?? null,
    time_of_day: bot.time?.timeOfDay ?? null,
    dimension: bot.game?.dimension ?? null,
    difficulty: bot.game?.difficulty ?? null,
    game_mode: bot.game?.gameMode ?? null
  };
}

/** Return the currently equipped items without requiring Mineflayer version-specific types. */
function equipmentSnapshot(bot: Bot) {
  return {
    main_hand: equipmentItem(bot, "hand"),
    off_hand: equipmentItem(bot, "off-hand"),
    head: equipmentItem(bot, "head"),
    chest: equipmentItem(bot, "torso"),
    legs: equipmentItem(bot, "legs"),
    feet: equipmentItem(bot, "feet")
  };
}

/** Read one equipment destination from Mineflayer inventory slots. */
function equipmentItem(bot: Bot, destination: string) {
  try {
    const slot = (bot as unknown as { getEquipmentDestSlot?: (destination: string) => number }).getEquipmentDestSlot?.(destination);
    if (typeof slot !== "number") {
      return null;
    }
    const item = bot.inventory.slots[slot];
    return item ? { name: item.name, count: item.count } : null;
  } catch {
    return null;
  }
}

/** Convert Mineflayer statistics into stable verifier-facing buckets. */
function normalizeStatistics(raw: unknown): Record<string, unknown> {
  const result: Record<string, unknown> = {
    kill_entity: {},
    use_item: {},
    custom: {}
  };
  visitStat(raw, [], result);
  return result;
}

/** Walk unknown Mineflayer stat shapes and extract common kill/use/custom counters. */
function visitStat(value: unknown, path: string[], result: Record<string, unknown>): void {
  if (typeof value === "number" && Number.isFinite(value)) {
    recordStat(path, value, result);
    return;
  }
  if (!value || typeof value !== "object") {
    return;
  }
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    visitStat(child, [...path, key], result);
  }
}

/** Record one numeric statistic in normalized buckets plus a flat fallback key. */
function recordStat(path: string[], value: number, result: Record<string, unknown>): void {
  const normalizedPath = path.map(normalizeStatKey);
  const joined = normalizedPath.join("/");
  const leaf = normalizedPath[normalizedPath.length - 1] ?? joined;
  if (joined.includes("kill_entity") || joined.includes("killed")) {
    (result.kill_entity as Record<string, number>)[leaf] = value;
  } else if (joined.includes("use_item") || joined.includes("used")) {
    (result.use_item as Record<string, number>)[leaf] = value;
  } else if (joined.includes("custom")) {
    (result.custom as Record<string, number>)[leaf] = value;
  }
  result[joined] = value;
}

/** Strip Minecraft namespace prefixes from stat keys for easier verifier matching. */
function normalizeStatKey(key: string): string {
  return key.replace(/^minecraft[:/]/, "");
}

/** Use Mineflayer visibility helpers when available, otherwise keep observation permissive. */
function canSeeEntity(bot: Bot, entity: Bot["entity"]): boolean {
  const maybeBot = bot as unknown as { canSeeEntity?: (target: Bot["entity"]) => boolean };
  return maybeBot.canSeeEntity?.(entity) ?? true;
}
