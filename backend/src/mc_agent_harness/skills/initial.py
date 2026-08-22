from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from mc_agent_harness.db.models import SKILL_DELETED_STATUS, SkillRecord
from mc_agent_harness.db.session import SessionFactory, SessionLocal
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus


@dataclass(frozen=True, slots=True)
class InitialSkillSeedResult:
    """Count of bootstrap skills inserted or refreshed in the authoritative skill table."""

    created: int
    updated: int
    unchanged: int


def initial_skill_specs() -> tuple[SkillSpec, ...]:
    """Return promoted bootstrap skills that encode non-obvious navigation techniques."""

    return (
        SkillSpec(
            name="recover_unreachable_by_digging",
            version="0.1.0",
            description=(
                "Recover from no_path or unreachable terrain by using scan evidence, moving to "
                "a reachable nearby coordinate, and deliberately digging blocks instead of "
                "repeating the same unreachable move_to target."
            ),
            triggers=[
                "blocked",
                "dig_block_at",
                "height_gap",
                "nearest_reachable_position",
                "no_path",
                "path_timeout",
                "scan_blocks",
                "target_unreachable",
                "unreachable",
            ],
            preconditions=[
                "A previous move_to or move_to_and_engage_combat result reported no_path, target_unreachable, or path_timeout.",
                "The target or blocking terrain coordinate is supported by scan_blocks, nearby_blocks, or compact_evidence.",
                "dig_block_at is allowed and the target block is not protected by task constraints.",
            ],
            strategy_summary=(
                "When direct navigation fails, do not keep moving to the exact same unreachable "
                "coordinate. Use nearest_reachable_position when present, rescan nearby terrain, "
                "then dig a specific blocking or target block with dig_block_at only when the "
                "coordinate is supported by evidence. After digging, wait briefly, rescan, and "
                "retry movement or pickup from a reachable ground coordinate."
            ),
            parameterized_plan=[
                {
                    "step": "read_failure_evidence",
                    "description": "Inspect previous_step.error_code, progress_status, nearest_reachable_position, and path_summary.",
                },
                {
                    "step": "choose_reachable_coordinate",
                    "description": "If nearest_reachable_position exists, move there before retrying target interaction.",
                },
                {
                    "step": "scan_or_dig",
                    "description": "Use scan_blocks to identify blocks around the target or path; use dig_block_at only for evidence-backed coordinates.",
                },
                {
                    "step": "verify_progress",
                    "description": "After terrain changes, wait briefly, rescan, and avoid repeating unchanged no_path actions.",
                },
            ],
            recovery_policy=[
                "If repeated no_path results have the same target and final_distance, change strategy instead of retrying.",
                "If no blocking coordinate is known, scan nearby blocks or move to nearest_reachable_position first.",
                "Do not dig arbitrary blocks without coordinate evidence from observation, scan_blocks, or previous action result.",
            ],
            source_evidence={
                "source": "bootstrap",
                "reason": "Minecraft terrain often blocks direct pathfinding; digging is a reusable navigation recovery tactic.",
            },
            verifier_stats={"bootstrap": True, "human_reviewed": True},
            action_plan=[
                HarnessAction(type="scan_blocks", args={"block": "<target_or_blocker>", "max_distance": "<radius>"}),
                HarnessAction(type="move_to", args={"position": "<nearest_reachable_position>"}),
                HarnessAction(type="dig_block_at", args={"position": "<evidence_backed_block_position>"}),
                HarnessAction(type="wait_ticks", args={"ticks": 10}),
            ],
            validation={
                "semantics": "contextual_guidance_not_macro_execution",
                "safety": "Only use evidence-backed coordinates; the skill is not an automatic excavation macro.",
            },
            task_scope=[
                "navigation",
                "terrain_recovery",
                "action:scan_blocks",
                "action:move_to",
                "action:dig_block_at",
                "action:wait_ticks",
            ],
            dependencies=[
                "action:scan_blocks",
                "action:move_to",
                "action:dig_block_at",
                "action:wait_ticks",
                "no_path",
                "unreachable",
            ],
            metrics={"usage_count": 0, "failure_count": 0, "bootstrap": True},
            status=SkillStatus.promoted,
        ),
        SkillSpec(
            name="gain_height_by_pillaring",
            version="0.1.0",
            description=(
                "Recover from vertical height gaps by placing a block at or under the current "
                "position to gain elevation, then rescanning or retrying movement."
            ),
            triggers=[
                "bridge",
                "height_delta",
                "pillar",
                "place_block",
                "target_above",
                "vertical_gap",
                "unreachable",
            ],
            preconditions=[
                "The target is above the agent or previous path evidence shows a vertical gap.",
                "The inventory contains a placeable block such as dirt, cobblestone, planks, or logs.",
                "place_block is allowed and there is a valid support block near or below the bot.",
            ],
            strategy_summary=(
                "When the target is above or a one-block elevation change blocks progress, the "
                "agent can deliberately place a carried block to create a step or pillar. Query "
                "inventory if the placeable block is uncertain, place one block using place_block, "
                "then move or rescan from the new position. This is a navigation tactic, not a "
                "fixed build macro."
            ),
            parameterized_plan=[
                {
                    "step": "check_placeable_inventory",
                    "description": "Use current inventory evidence or query_inventory to pick a disposable placeable block.",
                },
                {
                    "step": "place_support_block",
                    "description": "Call place_block with the chosen item; omit position when placing near or below the bot is intended.",
                },
                {
                    "step": "reassess_height",
                    "description": "After placement, wait briefly, scan or observe again, then retry movement from the changed terrain.",
                },
            ],
            recovery_policy=[
                "If place_block returns missing_item, query inventory or gather a cheap block before trying again.",
                "If place_block returns no_support_block, move to a nearby solid support or choose a different target approach.",
                "Do not repeatedly place blocks without checking whether height_delta or reachability improved.",
            ],
            source_evidence={
                "source": "bootstrap",
                "reason": "Pillaring is a non-obvious Minecraft technique for resolving vertical navigation failures.",
            },
            verifier_stats={"bootstrap": True, "human_reviewed": True},
            action_plan=[
                HarnessAction(type="query_inventory", args={}),
                HarnessAction(type="place_block", args={"item": "<placeable_block>"}),
                HarnessAction(type="wait_ticks", args={"ticks": 5}),
                HarnessAction(type="move_to", args={"position": "<target_or_reachable_coordinate>"}),
            ],
            validation={
                "semantics": "contextual_guidance_not_macro_execution",
                "safety": "Use only when inventory and terrain evidence justify changing height.",
            },
            task_scope=[
                "navigation",
                "terrain_recovery",
                "vertical_navigation",
                "action:query_inventory",
                "action:place_block",
                "action:wait_ticks",
                "action:move_to",
            ],
            dependencies=[
                "action:query_inventory",
                "action:place_block",
                "action:wait_ticks",
                "action:move_to",
                "height_delta",
                "placeable_block",
            ],
            metrics={"usage_count": 0, "failure_count": 0, "bootstrap": True},
            status=SkillStatus.promoted,
        ),
    )


def seed_initial_skills(session_factory: SessionFactory = SessionLocal) -> InitialSkillSeedResult:
    """Idempotently upsert promoted bootstrap skills into the configured skill store."""

    created = 0
    updated = 0
    unchanged = 0
    with session_factory() as session:
        records = list(session.scalars(select(SkillRecord)).all())
        for spec in initial_skill_specs():
            origin = {"name": spec.name, "version": spec.version}
            payload = {
                **spec.model_dump(mode="json"),
                "_bootstrap_origin": origin,
            }
            record = next(
                (
                    item
                    for item in records
                    if item.name == spec.name and item.version == spec.version
                ),
                None,
            )
            if record is None:
                record = next(
                    (
                        item
                        for item in records
                        if isinstance(item.spec, dict)
                        and item.spec.get("_bootstrap_origin") == origin
                    ),
                    None,
                )
            if record is None:
                record = SkillRecord(
                    name=spec.name,
                    version=spec.version,
                    status=spec.status.value,
                    spec=payload,
                    source_run_id=None,
                )
                session.add(record)
                records.append(record)
                created += 1
                continue
            if record.status == SKILL_DELETED_STATUS or (
                isinstance(record.spec, dict)
                and record.spec.get("_dashboard_override") is True
            ):
                unchanged += 1
                continue
            if record.status != spec.status.value or record.spec != payload:
                record.status = spec.status.value
                record.spec = payload
                record.source_run_id = None
                updated += 1
            else:
                unchanged += 1
        session.commit()
    return InitialSkillSeedResult(created=created, updated=updated, unchanged=unchanged)
