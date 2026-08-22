import assert from "node:assert/strict";
import test from "node:test";
import type { Bot } from "mineflayer";
import type { Entity } from "prismarine-entity";
import { Vec3 } from "vec3";
import { isEntityAirborne } from "./entity-state.js";

/** Build one minimal entity carrying the current server-reported ground state. */
function entity(name: string, onGround: boolean): Entity {
  return { id: 1, name, type: "animal", onGround, position: new Vec3(4, 72, 4) } as unknown as Entity;
}

/** Build a bot whose block lookup reports solid support or air beneath the target. */
function botWithSupport(hasSupport: boolean): Bot {
  return {
    blockAt: () => ({ boundingBox: hasSupport ? "block" : "empty" })
  } as unknown as Bot;
}

test("does not classify a grounded chicken as airborne because of terrain height", () => {
  assert.equal(isEntityAirborne(botWithSupport(true), entity("chicken", true)), false);
});

test("treats a chicken jump as transient airborne state", () => {
  assert.equal(isEntityAirborne(botWithSupport(true), entity("chicken", false)), true);
});

test("detects a falling chicken when remote onGround metadata is stale", () => {
  assert.equal(isEntityAirborne(botWithSupport(false), entity("chicken", true)), true);
});

test("recognizes inherently flying entities even when onGround metadata is stale", () => {
  assert.equal(isEntityAirborne(botWithSupport(true), entity("phantom", true)), true);
});
