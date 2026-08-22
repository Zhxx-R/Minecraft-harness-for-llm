import type { Bot } from "mineflayer";
import { runtimeEntityName } from "./entity-state.js";

/** Mineflayer entity shape used by task-local combat attribution. */
type RuntimeEntity = Bot["entity"];

/** Combat mode that produced an outgoing attack attempt. */
type CombatMode = "melee" | "ranged";

/** Recent attack evidence retained until the target dies or leaves the worker view. */
interface PendingAttackEvidence {
  entityId: number;
  entityName: string;
  mode: CombatMode;
  firstAttackAt: number;
  lastAttackAt: number;
  attackCount: number;
  lastDamageByBotAt: number | null;
}

/** Definitive entity-death event correlated with a recent attack from this bot. */
export interface ConfirmedKillEvent {
  sequence: number;
  entity_id: number;
  entity: string;
  observed_at_ms: number;
  attribution: "direct_damage" | "recent_attack";
  confidence: "high" | "medium";
  mode: CombatMode;
  attack_count: number;
  last_attack_age_ms: number;
}

/** JSON-safe task-local counters exposed through every observation. */
export interface CombatTrackerSnapshot {
  kill_entity: Record<string, number>;
  confirmed_kill_events: ConfirmedKillEvent[];
  source: "mineflayer_entity_dead";
}

/** Maximum age for attributing an entityDead event to this bot's attack attempt. */
const ATTACK_ATTRIBUTION_WINDOW_MS = 30_000;

/** Tracker instance retained for each live Mineflayer bot. */
const TRACKERS = new WeakMap<Bot, CombatTracker>();

/** Track confirmed kills from Mineflayer entityDead events without relying on item drops. */
export class CombatTracker {
  private readonly pendingAttacks = new Map<number, PendingAttackEvidence>();
  private readonly killCounts = new Map<string, number>();
  private readonly killEvents: ConfirmedKillEvent[] = [];
  private readonly processedDeadEntityIds = new Set<number>();
  private sequence = 0;

  /** Stable event handlers retained so listeners can be removed on disconnect. */
  private readonly onEntityHurt = (entity: RuntimeEntity, source?: RuntimeEntity) => {
    const pending = this.pendingAttacks.get(entity.id);
    if (pending && source?.id === this.bot.entity.id) {
      pending.lastDamageByBotAt = Date.now();
    }
  };

  private readonly onEntityDead = (entity: RuntimeEntity) => {
    this.confirmDeath(entity);
  };

  private readonly onEntityGone = (entity: RuntimeEntity) => {
    this.pendingAttacks.delete(entity.id);
  };

  private readonly onBotEnd = () => {
    this.dispose();
  };

  /** Attach task-local combat listeners to one Mineflayer bot connection. */
  constructor(private readonly bot: Bot) {
    bot.on("entityHurt", this.onEntityHurt);
    bot.on("entityDead", this.onEntityDead);
    bot.on("entityGone", this.onEntityGone);
    bot.once("end", this.onBotEnd);
  }

  /** Record one outgoing attack attempt before Mineflayer sends it to the server. */
  markAttack(entity: RuntimeEntity, mode: CombatMode): void {
    const now = Date.now();
    const existing = this.pendingAttacks.get(entity.id);
    if (existing) {
      existing.lastAttackAt = now;
      existing.attackCount += 1;
      existing.mode = mode;
      return;
    }
    this.pendingAttacks.set(entity.id, {
      entityId: entity.id,
      entityName: runtimeEntityName(entity),
      mode,
      firstAttackAt: now,
      lastAttackAt: now,
      attackCount: 1,
      lastDamageByBotAt: null
    });
  }

  /** Return the confirmed task-local kill count for one canonical entity name. */
  killCount(entityName: string): number {
    return this.killCounts.get(normalizeEntityName(entityName)) ?? 0;
  }

  /** Return the latest monotonically increasing kill-event sequence. */
  currentSequence(): number {
    return this.sequence;
  }

  /** Return confirmed kill events created after a caller's action-local cursor. */
  eventsSince(sequence: number, entityName?: string): ConfirmedKillEvent[] {
    const normalized = entityName ? normalizeEntityName(entityName) : null;
    return this.killEvents.filter((event) => event.sequence > sequence && (!normalized || event.entity === normalized));
  }

  /** Return stable verifier-facing counters and bounded event evidence. */
  snapshot(): CombatTrackerSnapshot {
    return {
      kill_entity: Object.fromEntries(this.killCounts.entries()),
      confirmed_kill_events: this.killEvents.slice(-50).map((event) => ({ ...event })),
      source: "mineflayer_entity_dead"
    };
  }

  /** Remove listeners when the bot disconnects so a reset starts with a clean ledger. */
  dispose(): void {
    this.bot.removeListener("entityHurt", this.onEntityHurt);
    this.bot.removeListener("entityDead", this.onEntityDead);
    this.bot.removeListener("entityGone", this.onEntityGone);
    this.bot.removeListener("end", this.onBotEnd);
    TRACKERS.delete(this.bot);
  }

  /** Confirm only explicit entityDead events associated with a recent bot attack. */
  private confirmDeath(entity: RuntimeEntity): void {
    if (this.processedDeadEntityIds.has(entity.id)) {
      return;
    }
    const pending = this.pendingAttacks.get(entity.id);
    if (!pending) {
      return;
    }
    const now = Date.now();
    const lastAttackAgeMs = Math.max(0, now - pending.lastAttackAt);
    if (lastAttackAgeMs > ATTACK_ATTRIBUTION_WINDOW_MS) {
      this.pendingAttacks.delete(entity.id);
      return;
    }

    this.processedDeadEntityIds.add(entity.id);
    this.pendingAttacks.delete(entity.id);
    this.sequence += 1;
    const directDamage = pending.lastDamageByBotAt !== null;
    const event: ConfirmedKillEvent = {
      sequence: this.sequence,
      entity_id: entity.id,
      entity: pending.entityName,
      observed_at_ms: now,
      attribution: directDamage ? "direct_damage" : "recent_attack",
      confidence: directDamage ? "high" : "medium",
      mode: pending.mode,
      attack_count: pending.attackCount,
      last_attack_age_ms: lastAttackAgeMs
    };
    this.killEvents.push(event);
    this.killCounts.set(pending.entityName, (this.killCounts.get(pending.entityName) ?? 0) + 1);
  }
}

/** Return or lazily install the combat tracker for one bot connection. */
export function combatTrackerFor(bot: Bot): CombatTracker {
  const existing = TRACKERS.get(bot);
  if (existing) {
    return existing;
  }
  const tracker = new CombatTracker(bot);
  TRACKERS.set(bot, tracker);
  return tracker;
}

/** Normalize task/entity ids before using them as ledger keys. */
function normalizeEntityName(name: string): string {
  return name.replace(/^minecraft:/, "").toLowerCase();
}
