/** One target-distance sample observed by the real-time combat controller. */
export interface CombatReachabilitySample {
  distance: number;
  heightDelta: number;
  targetAirborne: boolean;
  meleeReachable: boolean;
}

/** JSON-safe diagnostics returned when a bounded combat approach cannot reach its target. */
export interface CombatReachabilitySnapshot {
  reachability_scope: "current_engagement";
  tracking_duration_ms: number;
  unreachable_timeout_ms: number;
  stalled_for_ms: number;
  initial_distance: number | null;
  closest_distance: number | null;
  final_distance: number | null;
  distance_progress: number | null;
  initial_height_delta: number | null;
  final_height_delta: number | null;
  target_airborne: boolean | null;
  melee_reachable: boolean | null;
  follow_updates: number;
}

/** Minimum distance improvement that resets the no-progress timer. */
const DISTANCE_PROGRESS_EPSILON = 0.5;

/** Track whether dynamic following is still making meaningful melee progress. */
export class CombatReachabilityTracker {
  private initialSample: CombatReachabilitySample | null = null;
  private latestSample: CombatReachabilitySample | null = null;
  private closestDistance: number | null = null;
  private lastProgressAt: number;
  private followUpdates = 0;

  /** Create one action-local tracker using monotonic-compatible millisecond timestamps. */
  constructor(
    private readonly startedAt: number,
    private readonly unreachableTimeoutMs: number
  ) {
    this.lastProgressAt = startedAt;
  }

  /** Record current target reachability and reset the stall clock on real progress. */
  observe(sample: CombatReachabilitySample, observedAt: number): void {
    if (this.initialSample === null) {
      this.initialSample = { ...sample };
      this.closestDistance = sample.distance;
      this.lastProgressAt = observedAt;
    } else if (
      this.closestDistance === null ||
      sample.distance <= this.closestDistance - DISTANCE_PROGRESS_EPSILON
    ) {
      this.closestDistance = sample.distance;
      this.lastProgressAt = observedAt;
    }
    if (sample.meleeReachable) {
      this.lastProgressAt = observedAt;
    }
    this.latestSample = { ...sample };
  }

  /** Count one dynamic pathfinder goal refresh toward the tracked entity. */
  markFollowUpdate(): void {
    this.followUpdates += 1;
  }

  /** An actual attack proves temporary reachability and resets the stall clock. */
  markAttack(attackedAt: number): void {
    this.lastProgressAt = attackedAt;
  }

  /** Return true only after a bounded no-progress interval outside melee range. */
  shouldDeclareMeleeUnreachable(now: number): boolean {
    return (
      this.latestSample !== null &&
      !this.latestSample.meleeReachable &&
      now - this.lastProgressAt >= this.unreachableTimeoutMs
    );
  }

  /** Build compact evidence explaining the current engagement's reachability result. */
  snapshot(now: number): CombatReachabilitySnapshot {
    const initialDistance = this.initialSample?.distance ?? null;
    const finalDistance = this.latestSample?.distance ?? null;
    return {
      reachability_scope: "current_engagement",
      tracking_duration_ms: Math.max(0, now - this.startedAt),
      unreachable_timeout_ms: this.unreachableTimeoutMs,
      stalled_for_ms: Math.max(0, now - this.lastProgressAt),
      initial_distance: initialDistance,
      closest_distance: this.closestDistance,
      final_distance: finalDistance,
      distance_progress:
        initialDistance === null || this.closestDistance === null
          ? null
          : initialDistance - this.closestDistance,
      initial_height_delta: this.initialSample?.heightDelta ?? null,
      final_height_delta: this.latestSample?.heightDelta ?? null,
      target_airborne: this.latestSample?.targetAirborne ?? null,
      melee_reachable: this.latestSample?.meleeReachable ?? null,
      follow_updates: this.followUpdates
    };
  }
}
