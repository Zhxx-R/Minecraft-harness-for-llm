import type { Bot } from "mineflayer";

/** Mineflayer entity shape shared by observation and action diagnostics. */
type RuntimeEntity = Bot["entity"];

/** Entities whose normal movement is inherently airborne even if onGround is stale. */
const INHERENTLY_FLYING_ENTITY_NAMES = new Set([
  "bat",
  "blaze",
  "ender_dragon",
  "ghast",
  "phantom",
  "vex"
]);

/** Return a normalized canonical entity name for counters and audit records. */
export function runtimeEntityName(entity: RuntimeEntity): string {
  return String(entity.name ?? entity.type ?? "unknown")
    .replace(/^minecraft:/, "")
    .toLowerCase();
}

/** Vertical offset below entity feet used to inspect physical block support. */
const SUPPORT_BLOCK_OFFSET_Y = -0.25;

/** Report current airborne state without confusing supported high terrain with flight. */
export function isEntityAirborne(bot: Bot, entity: RuntimeEntity): boolean {
  const name = runtimeEntityName(entity);
  if (entity.onGround === false || INHERENTLY_FLYING_ENTITY_NAMES.has(name)) {
    return true;
  }
  const supportPosition = entity.position.offset(0, SUPPORT_BLOCK_OFFSET_Y, 0).floored();
  const supportBlock = bot.blockAt(supportPosition);
  if (!supportBlock) {
    return false;
  }
  return supportBlock.boundingBox === "empty";
}
