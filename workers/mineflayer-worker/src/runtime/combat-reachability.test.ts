import assert from "node:assert/strict";
import test from "node:test";
import { CombatReachabilityTracker } from "./combat-reachability.js";

/** Build one unreachable melee sample at the requested distance. */
function unreachableSample(distance: number) {
  return {
    distance,
    heightDelta: 4,
    targetAirborne: false,
    meleeReachable: false
  };
}

test("declares unreachable only after dynamic following stops making progress", () => {
  const tracker = new CombatReachabilityTracker(0, 8_000);
  tracker.observe(unreachableSample(12), 0);
  tracker.markFollowUpdate();
  tracker.observe(unreachableSample(8), 5_000);

  assert.equal(tracker.shouldDeclareMeleeUnreachable(12_999), false);
  assert.equal(tracker.shouldDeclareMeleeUnreachable(13_000), true);
  assert.equal(tracker.snapshot(13_000).distance_progress, 4);
});

test("does not classify a target as unreachable after entering melee range", () => {
  const tracker = new CombatReachabilityTracker(0, 8_000);
  tracker.observe(unreachableSample(8), 0);
  tracker.observe(
    {
      distance: 2.5,
      heightDelta: 0,
      targetAirborne: false,
      meleeReachable: true
    },
    7_000
  );

  assert.equal(tracker.shouldDeclareMeleeUnreachable(20_000), false);
});

test("an attack resets the no-progress interval before an unreachable result", () => {
  const tracker = new CombatReachabilityTracker(0, 8_000);
  tracker.observe(unreachableSample(7), 0);
  tracker.markAttack(7_000);
  tracker.observe(unreachableSample(7), 7_100);

  assert.equal(tracker.shouldDeclareMeleeUnreachable(14_999), false);
  assert.equal(tracker.shouldDeclareMeleeUnreachable(15_000), true);
});
