from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mc_agent_harness.db.models import (
    LearningCandidateRecord,
    RunRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.db.session import SessionFactory, SessionLocal
from mc_agent_harness.observability.identity import enrich_event_payload, identity_from_task_spec
from mc_agent_harness.schemas.learning import (
    LearningCandidateKind,
    LearningCandidateSpec,
    LearningCandidateStatus,
)


DURABLE_FAILURE_STATUSES = frozenset(
    {
        "blocked",
        "cannot_reach",
        "missing_ingredient",
        "missing_inputs",
        "missing_station",
        "no_ammo",
        "no_line_of_sight",
        "no_path",
        "no_recipe",
        "no_space",
        "path_stopped",
        "path_timeout",
        "recipe_not_found",
        "target_lost",
        "target_unreachable",
        "timeout_no_progress",
        "weapon_not_equipped",
    }
)
EXCLUDED_RUN_STATUSES = frozenset(
    {"model_timeout", "task_timeout", "runtime_error", "verification_inconclusive"}
)
ACTIVE_CONTEXT_STATUSES = frozenset(
    {
        LearningCandidateStatus.hypothesized,
        LearningCandidateStatus.corroborated,
        LearningCandidateStatus.validated,
    }
)


@dataclass(frozen=True, slots=True)
class FailureLearningDecision:
    """Classifier result explaining whether one run contains durable learning evidence."""

    should_record: bool
    reason: str
    candidate: LearningCandidateSpec | None = None


@dataclass(frozen=True, slots=True)
class LearningCandidateSnapshot:
    """Immutable failure-hypothesis view shared by every run in one training batch."""

    revision: str
    captured_at: str
    candidates: tuple[LearningCandidateSpec, ...]

    async def search(
        self,
        task_spec: dict[str, Any],
        limit: int = 2,
    ) -> list[LearningCandidateSpec]:
        """Return only active hypotheses from the exact task family and target scope."""

        scope_key = task_scope_key(task_spec)
        matches = [
            candidate
            for candidate in self.candidates
            if candidate.scope_key == scope_key and candidate.status in ACTIVE_CONTEXT_STATUSES
        ]
        matches.sort(
            key=lambda candidate: (
                candidate.status == LearningCandidateStatus.validated,
                candidate.confidence,
                candidate.recovery_count,
                candidate.support_count,
            ),
            reverse=True,
        )
        return [candidate.model_copy(deep=True) for candidate in matches[: max(limit, 0)]]

    def to_json(self) -> dict[str, Any]:
        """Return compact snapshot metadata without duplicating failure evidence in task specs."""

        return {
            "revision": self.revision,
            "captured_at": self.captured_at,
            "candidate_count": len(self.candidates),
        }


class FailureClassifier:
    """Separate durable gameplay failures from infrastructure and stochastic noise."""

    def classify(
        self,
        run: RunRecord,
        steps: list[StepRecord],
        events: list[TrajectoryEventRecord],
    ) -> FailureLearningDecision:
        """Classify the latest durable failed action in a terminal failed run."""

        failure_step = _latest_durable_failure(steps)
        failure_status = (
            _failure_status(failure_step.action_result)
            if failure_step is not None and isinstance(failure_step.action_result, dict)
            else None
        )
        timeout_has_sufficient_navigation_evidence = (
            run.status == "task_timeout" and failure_status == "timeout_no_progress"
        )
        if run.status in EXCLUDED_RUN_STATUSES and not timeout_has_sufficient_navigation_evidence:
            return FailureLearningDecision(False, f"excluded_run_status:{run.status}")
        if failure_step is None:
            return FailureLearningDecision(False, "no_durable_gameplay_failure")
        return FailureLearningDecision(
            True,
            "durable_gameplay_failure",
            _candidate_from_failure(run, failure_step, events),
        )


class LearningCandidateStore:
    """Persist failure hypotheses and validate them only after successful recovery evidence."""

    def __init__(
        self,
        session_factory: SessionFactory = SessionLocal,
        classifier: FailureClassifier | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.classifier = classifier or FailureClassifier()

    async def capture_snapshot(self) -> LearningCandidateSnapshot:
        """Freeze candidate visibility before a parallel batch starts."""

        with self.session_factory() as session:
            records = session.scalars(
                select(LearningCandidateRecord)
                .where(
                    LearningCandidateRecord.status.in_(
                        [status.value for status in ACTIVE_CONTEXT_STATUSES]
                    )
                )
                .order_by(LearningCandidateRecord.signature)
            ).all()
            candidates = tuple(_record_to_spec(record) for record in records)
        revision_source = json.dumps(
            [candidate.model_dump(mode="json") for candidate in candidates],
            sort_keys=True,
            separators=(",", ":"),
        )
        return LearningCandidateSnapshot(
            revision=hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:16],
            captured_at=datetime.now(tz=UTC).isoformat(),
            candidates=candidates,
        )

    async def record_failure(self, run_id: str) -> FailureLearningDecision:
        """Upsert one final failed run without creating or modifying a skill."""

        with self.session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                return FailureLearningDecision(False, "run_not_found")
            steps = list(
                session.scalars(
                    select(StepRecord)
                    .where(StepRecord.run_id == run_id)
                    .order_by(StepRecord.step_index)
                ).all()
            )
            events = list(
                session.scalars(
                    select(TrajectoryEventRecord)
                    .where(TrajectoryEventRecord.run_id == run_id)
                    .order_by(TrajectoryEventRecord.id)
                ).all()
            )
            decision = self.classifier.classify(run, steps, events)
            if not decision.should_record or decision.candidate is None:
                _record_learning_event(
                    session,
                    run,
                    "learning_candidate_skipped",
                    {"source_run_id": run_id, "reason": decision.reason},
                )
                session.commit()
                return decision

            record, created = _upsert_failure_candidate(session, decision.candidate)
            event_type = "learning_candidate_created" if created else "learning_candidate_updated"
            persisted = _record_to_spec(record)
            _record_learning_event(
                session,
                run,
                event_type,
                {
                    "source_run_id": run_id,
                    "candidate": _audit_payload(persisted),
                    "skill_created": False,
                },
            )
            session.commit()
            return FailureLearningDecision(True, decision.reason, persisted)

    async def record_success(self, run_id: str) -> list[LearningCandidateSpec]:
        """Validate same-scope hypotheses when a successful run supplies recovery evidence."""

        with self.session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None or run.status != "succeeded":
                return []
            steps = list(
                session.scalars(
                    select(StepRecord)
                    .where(StepRecord.run_id == run_id)
                    .order_by(StepRecord.step_index)
                ).all()
            )
            events = list(
                session.scalars(
                    select(TrajectoryEventRecord)
                    .where(TrajectoryEventRecord.run_id == run_id)
                    .order_by(TrajectoryEventRecord.id)
                ).all()
            )
            scope_key = task_scope_key(run.task_spec)
            records = list(
                session.scalars(
                    select(LearningCandidateRecord)
                    .where(
                        LearningCandidateRecord.scope_key == scope_key,
                        LearningCandidateRecord.status.in_(
                            [
                                LearningCandidateStatus.observed.value,
                                LearningCandidateStatus.hypothesized.value,
                                LearningCandidateStatus.corroborated.value,
                                LearningCandidateStatus.validated.value,
                            ]
                        ),
                    )
                    .with_for_update()
                ).all()
            )

            direct_failure = _latest_durable_failure(steps)
            direct_signature: str | None = None
            if direct_failure is not None:
                direct_candidate = _candidate_from_failure(run, direct_failure, events)
                direct_signature = direct_candidate.signature
                direct_record, _created = _upsert_failure_candidate(session, direct_candidate)
                if (
                    direct_record.status != LearningCandidateStatus.promoted.value
                    and all(record.signature != direct_record.signature for record in records)
                ):
                    records.append(direct_record)

            validated: list[LearningCandidateSpec] = []
            for record in records:
                after_step_index = (
                    int(direct_failure.step_index)
                    if direct_failure is not None and record.signature == direct_signature
                    else None
                )
                recovery_actions = _recovery_actions(
                    steps,
                    record.action_type,
                    after_step_index=after_step_index,
                )
                if not recovery_actions:
                    continue
                recovery_ids = list(record.recovery_run_ids or [])
                if run_id not in recovery_ids:
                    recovery_ids.append(run_id)
                    record.recovery_count += 1
                record.recovery_run_ids = recovery_ids
                record.status = LearningCandidateStatus.validated.value
                record.confidence = max(float(record.confidence), 0.8)
                record.knowledge_refs = _merge_dict_lists(
                    list(record.knowledge_refs or []),
                    _knowledge_refs(events),
                )
                record.hypothesis = _validated_hypothesis(record, recovery_actions)
                evidence = dict(record.evidence or {})
                evidence["validated_recovery"] = {
                    "run_id": run_id,
                    "verifier_status": run.status,
                    "action_types": recovery_actions,
                }
                record.evidence = evidence
                spec = _record_to_spec(record)
                validated.append(spec)
                _record_learning_event(
                    session,
                    run,
                    "learning_candidate_validated",
                    {
                        "recovery_run_id": run_id,
                        "candidate": _audit_payload(spec),
                        "recovery_actions": recovery_actions,
                    },
                )
            session.commit()
            return validated

    async def mark_promoted(
        self,
        candidates: list[LearningCandidateSpec],
        *,
        skill_name: str,
        skill_version: str,
        audit_run_id: str,
    ) -> None:
        """Mark validated lessons represented by a promoted contextual skill."""

        if not candidates:
            return
        signatures = [candidate.signature for candidate in candidates]
        with self.session_factory() as session:
            run = session.get(RunRecord, audit_run_id)
            records = session.scalars(
                select(LearningCandidateRecord)
                .where(LearningCandidateRecord.signature.in_(signatures))
                .with_for_update()
            ).all()
            for record in records:
                record.status = LearningCandidateStatus.promoted.value
                evidence = dict(record.evidence or {})
                evidence["promoted_skill"] = {
                    "name": skill_name,
                    "version": skill_version,
                }
                record.evidence = evidence
            if run is not None:
                _record_learning_event(
                    session,
                    run,
                    "learning_candidates_promoted",
                    {
                        "candidate_signatures": signatures,
                        "skill": {"name": skill_name, "version": skill_version},
                    },
                )
            session.commit()


def task_scope_key(task_spec: dict[str, Any] | None) -> str:
    """Build a quantity-independent task family and canonical target key."""

    task = task_spec if isinstance(task_spec, dict) else {}
    category = _slug(str(task.get("category") or task.get("family") or "task"))
    target = _task_target(task) or _task_family_target(str(task.get("task_id") or "unknown"))
    return f"{category}:{_slug(target)}"


def learning_context_payload(candidate: LearningCandidateSpec) -> dict[str, Any]:
    """Expose a candidate as a scoped hypothesis, never as an authoritative instruction."""

    return {
        "signature": candidate.signature,
        "scope_key": candidate.scope_key,
        "status": candidate.status.value,
        "hypothesis": candidate.hypothesis,
        "failure_status": candidate.failure_status,
        "action_type": candidate.action_type,
        "target": candidate.target,
        "confidence": candidate.confidence,
        "support_count": candidate.support_count,
        "recovery_count": candidate.recovery_count,
        "semantics": "scoped_hypothesis_not_authoritative_instruction",
        "knowledge_refs": candidate.knowledge_refs[:3],
    }


def _candidate_from_failure(
    run: RunRecord,
    step: StepRecord,
    events: list[TrajectoryEventRecord],
) -> LearningCandidateSpec:
    """Build a normalized candidate from one durable failed action."""

    action = step.action if isinstance(step.action, dict) else {}
    result = step.action_result if isinstance(step.action_result, dict) else {}
    action_type = _slug(str(action.get("type") or "unknown_action"))
    failure_status = _failure_status(result) or "unknown_failure"
    scope_key = task_scope_key(run.task_spec)
    target = _task_target(run.task_spec)
    knowledge_refs = _knowledge_refs(events)
    status = (
        LearningCandidateStatus.hypothesized
        if knowledge_refs
        else LearningCandidateStatus.observed
    )
    signature = f"{scope_key}:{action_type}:{_slug(failure_status)}"
    return LearningCandidateSpec(
        signature=signature,
        scope_key=scope_key,
        kind=_candidate_kind(action_type, failure_status),
        status=status,
        hypothesis=_initial_hypothesis(scope_key, action_type, failure_status, bool(knowledge_refs)),
        failure_status=failure_status,
        action_type=action_type,
        target=target,
        confidence=0.35 if knowledge_refs else 0.2,
        evidence={
            "latest_failure": {
                "run_id": run.id,
                "step_index": int(step.step_index),
                "action": _safe_action_evidence(action),
                "result": _safe_result_evidence(result),
            }
        },
        knowledge_refs=knowledge_refs,
        source_run_ids=[run.id],
    )


def _upsert_failure_candidate(
    session: Any,
    candidate: LearningCandidateSpec,
) -> tuple[LearningCandidateRecord, bool]:
    """Merge repeated evidence under a stable signature while holding a row lock."""

    record = session.scalar(
        select(LearningCandidateRecord)
        .where(LearningCandidateRecord.signature == candidate.signature)
        .with_for_update()
    )
    if record is None:
        record = LearningCandidateRecord(
            signature=candidate.signature,
            scope_key=candidate.scope_key,
            kind=candidate.kind.value,
            status=candidate.status.value,
            hypothesis=candidate.hypothesis,
            failure_status=candidate.failure_status,
            action_type=candidate.action_type,
            target=candidate.target,
            support_count=candidate.support_count,
            recovery_count=candidate.recovery_count,
            contradiction_count=candidate.contradiction_count,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            knowledge_refs=candidate.knowledge_refs,
            source_run_ids=candidate.source_run_ids,
            recovery_run_ids=candidate.recovery_run_ids,
        )
        try:
            with session.begin_nested():
                session.add(record)
                session.flush()
            return record, True
        except IntegrityError:
            record = session.scalar(
                select(LearningCandidateRecord)
                .where(LearningCandidateRecord.signature == candidate.signature)
                .with_for_update()
            )
            if record is None:
                raise

    source_ids = list(record.source_run_ids or [])
    is_new_support = any(run_id not in source_ids for run_id in candidate.source_run_ids)
    if is_new_support:
        source_ids.extend(run_id for run_id in candidate.source_run_ids if run_id not in source_ids)
        record.support_count += 1
    record.source_run_ids = source_ids
    record.knowledge_refs = _merge_dict_lists(
        list(record.knowledge_refs or []),
        candidate.knowledge_refs,
    )
    evidence = dict(record.evidence or {})
    observations = list(evidence.get("supporting_failures") or [])
    latest = candidate.evidence.get("latest_failure")
    if is_new_support and isinstance(latest, dict):
        observations.append(latest)
    if observations:
        evidence["supporting_failures"] = observations[-10:]
    record.evidence = evidence
    if record.status not in {
        LearningCandidateStatus.validated.value,
        LearningCandidateStatus.promoted.value,
    }:
        if record.support_count >= 2:
            record.status = LearningCandidateStatus.corroborated.value
            record.confidence = max(float(record.confidence), 0.55)
        elif record.knowledge_refs:
            record.status = LearningCandidateStatus.hypothesized.value
            record.confidence = max(float(record.confidence), 0.35)
    return record, False


def _latest_durable_failure(steps: list[StepRecord]) -> StepRecord | None:
    """Find the latest failed action with a reusable gameplay-level diagnosis."""

    for step in reversed(steps):
        result = step.action_result if isinstance(step.action_result, dict) else {}
        status = _failure_status(result)
        if status == "timeout_no_progress" and not _sufficient_no_progress_evidence(step, steps):
            continue
        if status in DURABLE_FAILURE_STATUSES:
            return step
    return None


def _sufficient_no_progress_evidence(
    candidate: StepRecord,
    steps: list[StepRecord],
) -> bool:
    """Require repeated same-area static navigation stalls with pathfinder diagnostics."""

    action = candidate.action if isinstance(candidate.action, dict) else {}
    result = candidate.action_result if isinstance(candidate.action_result, dict) else {}
    if action.get("type") != "move_to" or not _distance_unchanged(result):
        return False
    if not _has_pathfinder_evidence(result):
        return False
    target = _action_position(action)
    if target is None:
        return False
    matching_attempts = 0
    for step in steps:
        step_action = step.action if isinstance(step.action, dict) else {}
        step_result = step.action_result if isinstance(step.action_result, dict) else {}
        if step_action.get("type") != "move_to":
            continue
        if _failure_status(step_result) != "timeout_no_progress":
            continue
        step_target = _action_position(step_action)
        if step_target is None or _position_distance(target, step_target) > 2.5:
            continue
        if not _distance_unchanged(step_result) or not _has_pathfinder_evidence(step_result):
            continue
        matching_attempts += 1
    return matching_attempts >= 2


def _distance_unchanged(result: dict[str, Any]) -> bool:
    """Return true when one full navigation attempt changed distance by at most half a block."""

    try:
        initial = float(result["initial_distance"])
        final = float(result["final_distance"])
        timeout_ms = float(result.get("timeout_ms") or 0)
    except (KeyError, TypeError, ValueError):
        return False
    return abs(initial - final) <= 0.5 and timeout_ms >= 8000


def _has_pathfinder_evidence(result: dict[str, Any]) -> bool:
    """Require worker path diagnostics so API latency is not misclassified as navigation."""

    return any(
        value not in (None, {}, [], "")
        for value in (
            result.get("path_summary"),
            result.get("nearest_reachable_position"),
            result.get("navigation_failure_reason"),
        )
    )


def _action_position(action: dict[str, Any]) -> dict[str, float] | None:
    """Extract an explicit static target coordinate from one move action."""

    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    value = args.get("position") if isinstance(args.get("position"), dict) else args
    try:
        return {axis: float(value[axis]) for axis in ("x", "y", "z")}
    except (KeyError, TypeError, ValueError):
        return None


def _position_distance(left: dict[str, float], right: dict[str, float]) -> float:
    """Return Euclidean distance between two normalized action target coordinates."""

    return sum((left[axis] - right[axis]) ** 2 for axis in ("x", "y", "z")) ** 0.5


def _failure_status(result: dict[str, Any]) -> str | None:
    """Normalize worker failure fields into one stable classifier value."""

    for key in ("error_code", "progress_status", "status", "reason_code"):
        value = result.get(key)
        if isinstance(value, str) and value:
            normalized = _slug(value)
            if normalized in DURABLE_FAILURE_STATUSES:
                return normalized
    diagnosis = result.get("diagnosis")
    if isinstance(diagnosis, dict):
        return _failure_status(diagnosis)
    return None


def _candidate_kind(action_type: str, failure_status: str) -> LearningCandidateKind:
    """Map action and failure semantics to a reviewable learning class."""

    if action_type == "move_to" or failure_status in {"no_path", "path_timeout", "path_stopped"}:
        return LearningCandidateKind.navigation_recovery
    if action_type in {
        "move_to_and_engage_combat",
        "engage_combat",
        "equip_item",
        "scan_entities",
    }:
        return LearningCandidateKind.combat_adaptation
    if action_type in {"process_item", "craft_item", "place_block"}:
        return LearningCandidateKind.processing_recovery
    if failure_status in {"missing_ingredient", "missing_inputs", "missing_station"}:
        return LearningCandidateKind.resource_strategy
    return LearningCandidateKind.tactical_recovery


def _recovery_actions(
    steps: list[StepRecord],
    failed_action_type: str,
    *,
    after_step_index: int | None = None,
) -> list[str]:
    """Return successful actions capable of adapting the failed operation's strategy."""

    families = {
        "move_to": {"scan_blocks", "dig_block_at", "place_block", "move_to"},
        "move_to_and_engage_combat": {
            "scan_entities",
            "equip_item",
            "consume_item",
            "use_item",
            "move_to_and_engage_combat",
            "engage_combat",
        },
        "engage_combat": {
            "scan_entities",
            "equip_item",
            "consume_item",
            "use_item",
            "move_to_and_engage_combat",
            "engage_combat",
        },
        "equip_item": {
            "query_inventory",
            "equip_item",
            "move_to_and_engage_combat",
            "engage_combat",
        },
        "process_item": {"query_inventory", "scan_blocks", "move_to", "place_block", "process_item"},
        "craft_item": {"query_inventory", "scan_blocks", "move_to", "place_block", "craft_item"},
        "place_block": {"scan_blocks", "move_to", "dig_block_at", "place_block"},
    }
    allowed = families.get(failed_action_type, {failed_action_type})
    recovered: list[str] = []
    for step in steps:
        if after_step_index is not None and int(step.step_index) <= after_step_index:
            continue
        action = step.action if isinstance(step.action, dict) else {}
        action_type = _slug(str(action.get("type") or ""))
        result = step.action_result if isinstance(step.action_result, dict) else {}
        if action_type in allowed and _result_succeeded(result):
            recovered.append(action_type)
    return list(dict.fromkeys(recovered))


def _result_succeeded(result: dict[str, Any]) -> bool:
    """Recognize successful worker results without assuming every action has an ok field."""

    if result.get("ok") is True:
        return True
    return str(result.get("status") or "").lower() in {
        "success",
        "succeeded",
        "completed",
        "target_killed",
        "arrived",
        "placed",
        "crafted",
        "processed",
    }


def _validated_hypothesis(
    record: LearningCandidateRecord,
    recovery_actions: list[str],
) -> str:
    """Describe only the recovery pattern demonstrated by verifier-backed evidence."""

    action_chain = " -> ".join(recovery_actions)
    return (
        f"For {record.scope_key}, when {record.action_type} returns {record.failure_status}, "
        f"re-observe and adapt with the validated recovery actions [{action_chain}] instead of "
        "repeating the unchanged failed action. A later run completed the task verifier."
    )


def _initial_hypothesis(
    scope_key: str,
    action_type: str,
    failure_status: str,
    has_knowledge: bool,
) -> str:
    """Create a cautious hypothesis that does not turn one failure into a claimed fact."""

    evidence_note = (
        "Relevant knowledge was retrieved, but the adaptation still requires successful validation."
        if has_knowledge
        else "The cause is not confirmed; retrieve relevant mechanics before changing strategy."
    )
    return (
        f"For {scope_key}, {action_type} returned {failure_status}. Do not treat this single event "
        f"as a reusable rule. {evidence_note}"
    )


def _knowledge_refs(events: list[TrajectoryEventRecord]) -> list[dict[str, Any]]:
    """Extract bounded source identities from agent-initiated knowledge calls."""

    references: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "knowledge_tool_call":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if result.get("ok") is False:
            continue
        reference = {
            "tool": result.get("tool") or (payload.get("action") or {}).get("type"),
            "query": result.get("query") or result.get("item"),
            "documents": [
                {"id": doc.get("id"), "title": doc.get("title")}
                for doc in result.get("docs", [])
                if isinstance(doc, dict) and (doc.get("id") or doc.get("title"))
            ][:5],
            "terms": [
                term.get("canonical_id")
                for term in result.get("terms", [])
                if isinstance(term, dict) and term.get("canonical_id")
            ][:5],
        }
        recipe = result.get("recipe")
        if isinstance(recipe, dict):
            reference["recipe"] = {
                "output": recipe.get("output"),
                "station": recipe.get("station"),
            }
        if any(value for value in reference.values()):
            references.append(reference)
    return _merge_dict_lists([], references)[:10]


def _task_target(task: dict[str, Any]) -> str | None:
    """Prefer verifier targets so quantity and biome variants share one learning scope."""

    category = str(task.get("category") or "").lower()
    key_order = {
        "combat": ("entity", "entity_id", "name", "item", "item_id"),
        "harvest": ("item", "item_id", "block", "block_id", "entity", "entity_id", "name"),
        "techtree": ("item", "item_id", "name", "block", "block_id"),
    }.get(category, ("item", "item_id", "entity", "entity_id", "block", "block_id", "name"))
    for field in ("verifier", "success_criteria"):
        target = _first_target(task.get(field), key_order)
        if target:
            return target
    return None


def _first_target(value: Any, key_order: tuple[str, ...]) -> str | None:
    """Find the first target field inside nested verifier structures."""

    if isinstance(value, list):
        for item in value:
            target = _first_target(item, key_order)
            if target:
                return target
        return None
    if not isinstance(value, dict):
        return None
    for key in key_order:
        target = value.get(key)
        if isinstance(target, str) and target:
            return target
    for key in ("all", "any"):
        target = _first_target(value.get(key), key_order)
        if target:
            return target
    return None


def _task_family_target(task_id: str) -> str:
    """Fall back to a quantity-independent task id token when verifier metadata is absent."""

    tokens = [token for token in _slug(task_id).split("_") if token and not token.isdigit()]
    ignored = {"minedojo", "harvest", "combat", "techtree", "survival", "task"}
    filtered = [token for token in tokens if token not in ignored]
    return "_".join(filtered[:3]) or "unknown"


def _safe_action_evidence(action: dict[str, Any]) -> dict[str, Any]:
    """Keep only action type and bounded arguments needed for later audit."""

    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    safe_args = {
        key: value
        for key, value in args.items()
        if key
        in {
            "block",
            "entity",
            "item",
            "mode",
            "position",
            "station",
            "tolerance",
        }
    }
    return {"type": action.get("type"), "args": safe_args}


def _safe_result_evidence(result: dict[str, Any]) -> dict[str, Any]:
    """Keep stable diagnostics while excluding large observations and volatile payloads."""

    keys = {
        "error_code",
        "final_distance",
        "initial_distance",
        "movement_policy",
        "progress_status",
        "requires_break_count",
        "requires_place_count",
        "state_summary",
        "status",
        "target_airborne",
        "target_height_delta",
    }
    return {key: result[key] for key in keys if key in result}


def _merge_dict_lists(
    current: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge JSON dictionaries deterministically without duplicate audit evidence."""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*current, *incoming]:
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _record_to_spec(record: LearningCandidateRecord) -> LearningCandidateSpec:
    """Convert one SQL record into its portable schema."""

    return LearningCandidateSpec(
        id=record.id,
        signature=record.signature,
        scope_key=record.scope_key,
        kind=LearningCandidateKind(record.kind),
        status=LearningCandidateStatus(record.status),
        hypothesis=record.hypothesis,
        failure_status=record.failure_status,
        action_type=record.action_type,
        target=record.target,
        support_count=record.support_count,
        recovery_count=record.recovery_count,
        contradiction_count=record.contradiction_count,
        confidence=record.confidence,
        evidence=dict(record.evidence or {}),
        knowledge_refs=list(record.knowledge_refs or []),
        source_run_ids=list(record.source_run_ids or []),
        recovery_run_ids=list(record.recovery_run_ids or []),
    )


def _audit_payload(candidate: LearningCandidateSpec) -> dict[str, Any]:
    """Return compact candidate state for trajectory events and reports."""

    return {
        "id": candidate.id,
        "signature": candidate.signature,
        "scope_key": candidate.scope_key,
        "kind": candidate.kind.value,
        "status": candidate.status.value,
        "failure_status": candidate.failure_status,
        "action_type": candidate.action_type,
        "target": candidate.target,
        "support_count": candidate.support_count,
        "recovery_count": candidate.recovery_count,
        "confidence": candidate.confidence,
        "source_run_ids": candidate.source_run_ids,
        "recovery_run_ids": candidate.recovery_run_ids,
        "knowledge_refs": candidate.knowledge_refs,
    }


def _record_learning_event(
    session: Any,
    run: RunRecord,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Persist an identity-complete learning event beside the source trajectory."""

    identity = identity_from_task_spec(run.task_spec)
    enriched, resolved = enrich_event_payload(payload, identity)
    session.add(
        TrajectoryEventRecord(
            run_id=run.id,
            event_type=event_type,
            payload=enriched,
            task_id=resolved.task_id or run.task_id,
            agent_id=resolved.agent_id,
        )
    )


def _slug(value: str) -> str:
    """Normalize identifiers used in signatures and task scopes."""

    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "unknown"
