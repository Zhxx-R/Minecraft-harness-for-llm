import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import type { Bot } from "mineflayer";
import type { Entity } from "prismarine-entity";
import { CombatTracker } from "./combat-tracker.js";

/** Build the minimal event-emitting bot shape required by CombatTracker. */
function fakeBot(): Bot {
  const emitter = new EventEmitter() as EventEmitter & { entity: Entity };
  emitter.entity = { id: 1, name: "player", type: "player" } as Entity;
  return emitter as unknown as Bot;
}

/** Build one target entity with stable identity for tracker tests. */
function fakeEntity(id: number, name: string): Entity {
  return { id, name, type: "animal" } as unknown as Entity;
}

test("counts entityDead after a direct bot attack without requiring drops", () => {
  const bot = fakeBot();
  const tracker = new CombatTracker(bot);
  const pig = fakeEntity(2, "pig");

  tracker.markAttack(pig, "melee");
  bot.emit("entityHurt", pig, bot.entity);
  bot.emit("entityDead", pig);

  assert.equal(tracker.killCount("pig"), 1);
  assert.equal(tracker.snapshot().confirmed_kill_events[0]?.attribution, "direct_damage");
  tracker.dispose();
});

test("does not count entityGone or an unrelated entityDead event as a kill", () => {
  const bot = fakeBot();
  const tracker = new CombatTracker(bot);
  const chicken = fakeEntity(3, "chicken");
  const unrelatedPig = fakeEntity(4, "pig");

  tracker.markAttack(chicken, "melee");
  bot.emit("entityGone", chicken);
  bot.emit("entityDead", unrelatedPig);

  assert.equal(tracker.killCount("chicken"), 0);
  assert.equal(tracker.killCount("pig"), 0);
  tracker.dispose();
});

test("deduplicates repeated death packets for the same attacked entity", () => {
  const bot = fakeBot();
  const tracker = new CombatTracker(bot);
  const zombie = fakeEntity(5, "zombie");

  tracker.markAttack(zombie, "ranged");
  bot.emit("entityDead", zombie);
  bot.emit("entityDead", zombie);

  assert.equal(tracker.killCount("zombie"), 1);
  assert.equal(tracker.eventsSince(0, "zombie").length, 1);
  tracker.dispose();
});
