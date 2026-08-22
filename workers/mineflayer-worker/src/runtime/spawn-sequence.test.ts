import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import type { Bot } from "mineflayer";
import { Vec3 } from "vec3";
import { observe } from "../observations/observe.js";
import { spawnSequence, trackSpawnSequence } from "./spawn-sequence.js";

function fakeBot(entityId: number) {
  const events = new EventEmitter();
  const entity = {
    id: entityId,
    position: new Vec3(12, 64, -8)
  };
  const bot = Object.assign(events, {
    entity,
    entities: { [entityId]: entity },
    health: 20,
    food: 20,
    inventory: {
      items: () => [],
      slots: []
    },
    findBlocks: () => [],
    time: {
      age: 100,
      timeOfDay: 1_000
    },
    game: {
      dimension: "overworld",
      difficulty: "normal",
      gameMode: "survival"
    }
  }) as unknown as Bot;
  return { bot, events };
}

test("spawn sequence advances once for every spawn event", () => {
  const { bot, events } = fakeBot(41);

  trackSpawnSequence(bot);
  trackSpawnSequence(bot);
  assert.equal(spawnSequence(bot), 0);

  events.emit("spawn");
  assert.equal(spawnSequence(bot), 1);

  events.emit("spawn");
  assert.equal(spawnSequence(bot), 2);
});

test("spawn sequences are isolated by bot connection", () => {
  const first = fakeBot(41);
  const second = fakeBot(42);
  trackSpawnSequence(first.bot);
  trackSpawnSequence(second.bot);

  first.events.emit("spawn");
  first.events.emit("spawn");
  second.events.emit("spawn");

  assert.equal(spawnSequence(first.bot), 2);
  assert.equal(spawnSequence(second.bot), 1);
});

test("observation exposes the current spawn sequence at its root", () => {
  const { bot, events } = fakeBot(41);
  trackSpawnSequence(bot);

  events.emit("spawn");
  assert.equal(observe(bot).spawn_sequence, 1);

  events.emit("spawn");
  assert.equal(observe(bot).spawn_sequence, 2);
});
