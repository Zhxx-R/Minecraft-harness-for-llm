import type { Bot } from "mineflayer";
import type { Vec3 } from "vec3";

/** Stable target identity retained while Mineflayer follows a moving entity. */
export interface PersistentFollowTarget {
  id: number;
  name: string;
  type: string;
  position: Vec3;
}

/** JSON-safe lifecycle state for one background follow session. */
export interface PersistentFollowSnapshot {
  active: boolean;
  target: {
    id: number;
    name: string;
    type: string;
    position: { x: number; y: number; z: number };
  };
  follow_distance: number;
  started_at_ms: number;
  elapsed_ms: number;
  until: "next_action_received";
}

/** JSON-safe evidence emitted when a subsequent action stops background following. */
export interface StoppedPersistentFollow extends PersistentFollowSnapshot {
  active: false;
  stopped_at_ms: number;
  stop_reason: "next_action_received" | "follow_replaced";
}

interface ActivePersistentFollow {
  target: PersistentFollowTarget;
  followDistance: number;
  startedAtMs: number;
}

const activeFollows = new WeakMap<Bot, ActivePersistentFollow>();

/**
 * Install a dynamic pathfinder goal that continues after the follow RPC returns.
 *
 * The caller owns the concrete GoalFollow instance so this lifecycle module can
 * stay independently testable without constructing Mineflayer pathfinder goals.
 */
export function startPersistentFollow(
  bot: Bot,
  target: PersistentFollowTarget,
  followDistance: number,
  goal: unknown,
  nowMs = Date.now()
): {
  active_follow: PersistentFollowSnapshot;
  replaced_follow: StoppedPersistentFollow | null;
} {
  const replacedFollow = stopPersistentFollow(bot, "follow_replaced", nowMs);
  bot.pathfinder.setGoal(goal as never, true);
  activeFollows.set(bot, {
    target,
    followDistance,
    startedAtMs: nowMs
  });
  return {
    active_follow: persistentFollowSnapshot(bot, nowMs) as PersistentFollowSnapshot,
    replaced_follow: replacedFollow
  };
}

/** Stop the current dynamic goal immediately before the next action executes. */
export function stopPersistentFollow(
  bot: Bot,
  reason: StoppedPersistentFollow["stop_reason"],
  nowMs = Date.now()
): StoppedPersistentFollow | null {
  const active = activeFollows.get(bot);
  if (!active) {
    return null;
  }

  activeFollows.delete(bot);
  bot.pathfinder.setGoal(null);
  bot.clearControlStates();
  return {
    ...snapshot(active, nowMs),
    active: false,
    stopped_at_ms: nowMs,
    stop_reason: reason
  };
}

/** Read current follow state for observations without interrupting movement. */
export function persistentFollowSnapshot(
  bot: Bot,
  nowMs = Date.now()
): PersistentFollowSnapshot | null {
  const active = activeFollows.get(bot);
  return active ? snapshot(active, nowMs) : null;
}

function snapshot(active: ActivePersistentFollow, nowMs: number): PersistentFollowSnapshot {
  return {
    active: true,
    target: {
      id: active.target.id,
      name: active.target.name,
      type: active.target.type,
      position: {
        x: active.target.position.x,
        y: active.target.position.y,
        z: active.target.position.z
      }
    },
    follow_distance: active.followDistance,
    started_at_ms: active.startedAtMs,
    elapsed_ms: Math.max(0, nowMs - active.startedAtMs),
    until: "next_action_received"
  };
}
