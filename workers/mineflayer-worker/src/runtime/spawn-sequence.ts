import type { Bot } from "mineflayer";

/** Spawn generation retained independently for each Mineflayer bot connection. */
const SPAWN_SEQUENCES = new WeakMap<Bot, number>();

/**
 * Start tracking the bot's initial spawn and every later respawn.
 *
 * Registration is idempotent so callers cannot accidentally install duplicate
 * listeners and advance the sequence more than once for a single spawn event.
 */
export function trackSpawnSequence(bot: Bot): void {
  if (SPAWN_SEQUENCES.has(bot)) {
    return;
  }
  SPAWN_SEQUENCES.set(bot, 0);
  bot.on("spawn", () => {
    SPAWN_SEQUENCES.set(bot, spawnSequence(bot) + 1);
  });
}

/** Return the number of spawn events observed for this bot connection. */
export function spawnSequence(bot: Bot): number {
  return SPAWN_SEQUENCES.get(bot) ?? 0;
}
