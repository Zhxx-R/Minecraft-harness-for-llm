import assert from "node:assert/strict";
import test from "node:test";
import type { Bot } from "mineflayer";
import { Vec3 } from "vec3";
import {
  persistentFollowSnapshot,
  startPersistentFollow,
  stopPersistentFollow
} from "./persistent-follow.js";

function fakeBot() {
  const goals: Array<{ goal: unknown; dynamic?: boolean }> = [];
  let clearCount = 0;
  const bot = {
    pathfinder: {
      setGoal: (goal: unknown, dynamic?: boolean) => {
        goals.push({ goal, dynamic });
      }
    },
    clearControlStates: () => {
      clearCount += 1;
    }
  } as unknown as Bot;
  return {
    bot,
    goals,
    clearCount: () => clearCount
  };
}

const sheep = {
  id: 143,
  name: "sheep",
  type: "animal",
  position: new Vec3(20, 70, 249)
};

test("follow remains active until a later action explicitly stops it", () => {
  const runtime = fakeBot();
  const goal = { kind: "GoalFollow", entityId: sheep.id };

  const started = startPersistentFollow(runtime.bot, sheep, 1.25, goal, 1_000);

  assert.deepEqual(runtime.goals, [{ goal, dynamic: true }]);
  assert.equal(started.active_follow.active, true);
  assert.equal(started.active_follow.until, "next_action_received");
  assert.equal(persistentFollowSnapshot(runtime.bot, 1_750)?.elapsed_ms, 750);
  assert.equal(runtime.clearCount(), 0);

  const stopped = stopPersistentFollow(runtime.bot, "next_action_received", 2_000);

  assert.equal(stopped?.target.id, sheep.id);
  assert.equal(stopped?.stop_reason, "next_action_received");
  assert.equal(stopped?.elapsed_ms, 1_000);
  assert.deepEqual(runtime.goals[1], { goal: null, dynamic: undefined });
  assert.equal(runtime.clearCount(), 1);
  assert.equal(persistentFollowSnapshot(runtime.bot, 2_100), null);
});

test("a second follow replaces the previous target before starting", () => {
  const runtime = fakeBot();
  const cow = {
    id: 144,
    name: "cow",
    type: "animal",
    position: new Vec3(8, 65, 8)
  };

  startPersistentFollow(runtime.bot, sheep, 1.25, { target: "sheep" }, 100);
  const replacement = startPersistentFollow(
    runtime.bot,
    cow,
    2,
    { target: "cow" },
    500
  );

  assert.equal(replacement.replaced_follow?.target.id, sheep.id);
  assert.equal(replacement.replaced_follow?.stop_reason, "follow_replaced");
  assert.equal(replacement.active_follow.target.id, cow.id);
  assert.deepEqual(runtime.goals, [
    { goal: { target: "sheep" }, dynamic: true },
    { goal: null, dynamic: undefined },
    { goal: { target: "cow" }, dynamic: true }
  ]);
});
