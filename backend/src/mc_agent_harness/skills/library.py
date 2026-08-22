from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from mc_agent_harness.db.models import (
    SKILL_DELETED_STATUS,
    RunRecord,
    SkillRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.db.session import SessionFactory, SessionLocal
from mc_agent_harness.observability.identity import (
    AuditIdentity,
    enrich_event_payload,
    identity_from_task_spec,
)
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.schemas.learning import LearningCandidateSpec, LearningCandidateStatus
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus, SkillStepRange
from mc_agent_harness.skills.creation import (
    SkillCreationDecision,
    SkillCreationPolicy,
    SkillSummarizer,
    select_relevant_skill_steps,
)
from mc_agent_harness.skills.dedup import SkillCandidateDeduper, SkillDuplicateMatch


class SkillLibraryError(RuntimeError):
    """Raised when a skill library operation cannot be completed safely."""


PROMOTABLE_ACTION_TYPES = frozenset(
    {
        "scan_blocks",
        "scan_dropped_items",
        "move_to",
        "follow",
        "dig_block_at",
        "wait_ticks",
        "process_item",
        "craft_item",
        "smelt_item",
        "place_block",
        "equip_item",
        "scan_entities",
        "engage_combat",
        "consume_item",
        "move_to_and_engage_combat",
        "fight_entity",
        "use_item",
    }
)


@dataclass(frozen=True, slots=True)
class SkillSearchScope:
    """Optional structured search hints used by the multi-level skill index."""

    task_id: str | None = None
    task_tags: tuple[str, ...] = ()
    canonical_ids: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    task_terms: tuple[str, ...] = ()
    priority_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillSearchMatch:
    """One ranked skill plus an auditable normalized relevance score."""

    skill: SkillSpec
    raw_score: int
    relevance: float
    matched_task_terms: tuple[str, ...] = ()
    matched_priority_terms: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return bounded retrieval evidence without expanding the full Skill spec."""

        return {
            "name": self.skill.name,
            "version": self.skill.version,
            "raw_score": self.raw_score,
            "relevance": self.relevance,
            "matched_task_terms": list(self.matched_task_terms),
            "matched_priority_terms": list(self.matched_priority_terms),
        }


@dataclass(frozen=True, slots=True)
class SkillLibrarySnapshot:
    """Immutable promoted-skill view shared by every run in one training batch."""

    revision: str
    captured_at: str
    skills: tuple[SkillSpec, ...]

    async def search(
        self,
        query: str,
        scope: SkillSearchScope | dict[str, Any] | None = None,
        limit: int = 3,
    ) -> list[SkillSpec]:
        """Search only skills that existed when this batch snapshot was captured."""

        matches = await self.search_ranked(query, scope=scope, limit=limit)
        return [match.skill for match in matches]

    async def search_ranked(
        self,
        query: str,
        scope: SkillSearchScope | dict[str, Any] | None = None,
        limit: int = 3,
    ) -> list[SkillSearchMatch]:
        """Return immutable-snapshot matches with raw and normalized relevance."""

        matches = _rank_skill_specs(self.skills, query, _coerce_scope(scope), limit)
        return [
            SkillSearchMatch(
                skill=match.skill.model_copy(deep=True),
                raw_score=match.raw_score,
                relevance=match.relevance,
                matched_task_terms=match.matched_task_terms,
                matched_priority_terms=match.matched_priority_terms,
            )
            for match in matches
        ]

    def to_json(self) -> dict[str, Any]:
        """Return compact snapshot metadata for task specs and audit events."""

        return {
            "revision": self.revision,
            "captured_at": self.captured_at,
            "skill_count": len(self.skills),
            "skills": [
                {
                    "name": skill.name,
                    "version": skill.version,
                    "status": skill.status.value,
                }
                for skill in self.skills
            ],
        }


class SkillLibrary:
    """Database-backed authoritative skill metadata, retrieval, and version lookup."""

    def __init__(
        self,
        session_factory: SessionFactory = SessionLocal,
        export_dir: str | Path = "skills/exports",
        creation_policy: SkillCreationPolicy | None = None,
        summarizer: SkillSummarizer | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.export_dir = Path(export_dir)
        self.creation_policy = creation_policy or SkillCreationPolicy()
        self.summarizer = summarizer or SkillSummarizer()

    async def capture_snapshot(
        self,
        statuses: tuple[SkillStatus, ...] = (SkillStatus.promoted,),
    ) -> SkillLibrarySnapshot:
        """Freeze the skills visible to all model contexts in one training batch."""

        with self.session_factory() as session:
            records = session.scalars(
                select(SkillRecord)
                .where(SkillRecord.status.in_([status.value for status in statuses]))
                .order_by(SkillRecord.name, SkillRecord.version)
            ).all()
            skills = tuple(_record_to_spec(record).model_copy(deep=True) for record in records)
        revision_source = json.dumps(
            [skill.model_dump(mode="json") for skill in skills],
            sort_keys=True,
            separators=(",", ":"),
        )
        revision = hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:16]
        return SkillLibrarySnapshot(
            revision=revision,
            captured_at=datetime.now(tz=UTC).isoformat(),
            skills=skills,
        )

    async def search(
        self,
        query: str,
        scope: SkillSearchScope | dict[str, Any] | None = None,
        limit: int = 3,
        statuses: tuple[SkillStatus, ...] = (SkillStatus.promoted,),
        audit_run_id: str | None = None,
    ) -> list[SkillSpec]:
        """Search skills with exact trigger, task tag, action-scope, dependency, and lexical scores."""

        matches = await self.search_ranked(
            query,
            scope=scope,
            limit=limit,
            statuses=statuses,
            audit_run_id=audit_run_id,
        )
        return [match.skill for match in matches]

    async def search_ranked(
        self,
        query: str,
        scope: SkillSearchScope | dict[str, Any] | None = None,
        limit: int = 3,
        statuses: tuple[SkillStatus, ...] = (SkillStatus.promoted,),
        audit_run_id: str | None = None,
    ) -> list[SkillSearchMatch]:
        """Search skills and retain normalized relevance evidence for thresholding."""

        search_scope = _coerce_scope(scope)
        with self.session_factory() as session:
            records = session.scalars(
                select(SkillRecord).where(
                    SkillRecord.status.in_([status.value for status in statuses])
                )
            ).all()
            matches = _rank_skill_specs(
                [_record_to_spec(record) for record in records],
                query,
                search_scope,
                limit,
            )
            if audit_run_id is not None:
                _record_skill_event(
                    session,
                    audit_run_id,
                    "skill_search",
                    {
                        "query": query,
                        "scope": _scope_payload(search_scope),
                        "results": [match.to_json() for match in matches],
                    },
                )
                session.commit()
            return matches

    async def get(
        self,
        name: str,
        version: str | None = None,
        audit_run_id: str | None = None,
    ) -> SkillSpec | None:
        """Load a skill by name and optional version, preferring the newest non-deprecated version."""

        with self.session_factory() as session:
            statement: Select[tuple[SkillRecord]] = select(SkillRecord).where(
                SkillRecord.name == name,
                SkillRecord.status != SKILL_DELETED_STATUS,
            )
            if version is not None:
                statement = statement.where(SkillRecord.version == version)
            else:
                statement = statement.where(SkillRecord.status != SkillStatus.deprecated.value)
            records = session.scalars(statement).all()
            if not records:
                return None
            record = sorted(records, key=lambda item: _version_key(item.version), reverse=True)[0]
            spec = _record_to_spec(record)
            if audit_run_id is not None:
                _record_skill_event(
                    session,
                    audit_run_id,
                    "skill_read",
                    {"name": spec.name, "version": spec.version, "status": spec.status.value},
                )
                session.commit()
            return spec

    async def find_duplicates(
        self,
        candidate: SkillSpec,
        *,
        threshold: float = 0.82,
        statuses: tuple[SkillStatus, ...] = (
            SkillStatus.draft,
            SkillStatus.validated,
            SkillStatus.staged,
            SkillStatus.promoted,
        ),
        audit_run_id: str | None = None,
    ) -> list[SkillDuplicateMatch]:
        """Find near-duplicate skills before promotion or candidate review."""

        with self.session_factory() as session:
            records = session.scalars(
                select(SkillRecord).where(
                    SkillRecord.status.in_([status.value for status in statuses])
                )
            ).all()
            existing = [_record_to_spec(record) for record in records]
            matches = SkillCandidateDeduper().find_duplicates(
                candidate,
                existing,
                threshold=threshold,
            )
            if audit_run_id is not None:
                _record_skill_event(
                    session,
                    audit_run_id,
                    "skill_duplicate_check",
                    {
                        "candidate": {"name": candidate.name, "version": candidate.version},
                        "threshold": threshold,
                        "matches": [
                            {
                                "name": match.skill.name,
                                "version": match.skill.version,
                                "similarity": match.similarity,
                            }
                            for match in matches
                        ],
                    },
                )
                session.commit()
            return matches

    async def create_candidate(
        self,
        run_id: str,
        name: str | None = None,
        description: str | None = None,
        audit_run_id: str | None = None,
        learning_candidates: Sequence[LearningCandidateSpec] | None = None,
    ) -> SkillSpec:
        """Create a draft skill candidate from a successful persisted run trajectory."""

        with self.session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                raise SkillLibraryError(f"Cannot create skill candidate; run not found: {run_id}")
            existing_record = session.scalar(
                select(SkillRecord)
                .where(
                    SkillRecord.source_run_id == run_id,
                    SkillRecord.status != SKILL_DELETED_STATUS,
                )
                .order_by(SkillRecord.id)
            )
            if existing_record is not None:
                return _record_to_spec(existing_record)
            steps = session.scalars(
                select(StepRecord)
                .where(StepRecord.run_id == run_id)
                .order_by(StepRecord.step_index)
            ).all()
            evidence_selection = select_relevant_skill_steps(run, list(steps))
            successful_steps = _successful_progress_steps(evidence_selection.steps)
            if not successful_steps:
                raise SkillLibraryError(
                    f"Cannot create skill candidate; run has no successful progress actions: {run_id}"
                )

            action_plan = [HarnessAction.model_validate(step.action) for step in successful_steps]
            decision = self.creation_policy.evaluate(
                run, evidence_selection.steps, successful_steps
            )
            validated_learning = [
                candidate
                for candidate in learning_candidates or []
                if candidate.status
                in {LearningCandidateStatus.validated, LearningCandidateStatus.promoted}
                and run_id in candidate.recovery_run_ids
            ]
            if not decision.should_create and validated_learning:
                decision = SkillCreationDecision(
                    should_create=True,
                    reason="validated_failure_recovery",
                    evidence={
                        **decision.evidence,
                        "learning_candidate_signatures": [
                            candidate.signature for candidate in validated_learning
                        ],
                        "overrode_policy_reason": decision.reason,
                    },
                )
            if not decision.should_create:
                _record_skill_event(
                    session,
                    audit_run_id or run_id,
                    "skill_candidate_policy_skipped",
                    {
                        "reason": decision.reason,
                        "evidence": decision.evidence,
                        "evidence_filter": {
                            "verifier_entity_target": evidence_selection.verifier_entity_target,
                            "excluded_steps": evidence_selection.excluded_steps,
                        },
                        "source_run_id": run_id,
                    },
                )
                session.commit()
                raise SkillLibraryError(
                    f"Cannot create skill candidate; policy skipped run {run_id}: {decision.reason}"
                )
            summary = self.summarizer.summarize(
                run,
                action_plan,
                successful_steps,
                decision,
                excluded_steps=evidence_selection.excluded_steps,
            )
            candidate_name = name or summary.name
            version = _next_version(session, candidate_name)
            learning_evidence = [_learning_candidate_payload(item) for item in validated_learning]
            strategy_summary = summary.strategy_summary
            recovery_policy = list(summary.recovery_policy)
            source_evidence = dict(summary.source_evidence)
            validation = dict(summary.validation)
            triggers = list(summary.triggers)
            if validated_learning:
                strategy_summary = (
                    f"{strategy_summary} Validated recovery lesson: "
                    + " ".join(item.hypothesis for item in validated_learning)
                ).strip()
                recovery_policy.extend(
                    item.hypothesis
                    for item in validated_learning
                    if item.hypothesis not in recovery_policy
                )
                source_evidence["learning_candidates"] = learning_evidence
                validation["failure_learning_gate"] = "validated_by_successful_verifier"
                validation["learning_candidates"] = learning_evidence
                triggers.extend(
                    trigger
                    for item in validated_learning
                    for trigger in (item.failure_status, item.target)
                    if trigger and trigger not in triggers
                )
            source_range = SkillStepRange(
                start=int(successful_steps[0].step_index),
                end=int(successful_steps[-1].step_index),
            )
            spec = SkillSpec(
                name=candidate_name,
                version=version,
                description=description or summary.description,
                triggers=triggers,
                preconditions=summary.preconditions,
                strategy_summary=strategy_summary,
                parameterized_plan=summary.parameterized_plan,
                recovery_policy=recovery_policy,
                source_evidence=source_evidence,
                verifier_stats=summary.verifier_stats,
                action_plan=action_plan,
                validation={
                    **validation,
                    "promotable_action_types": sorted(PROMOTABLE_ACTION_TYPES),
                },
                source_run_id=run_id,
                source_step_range=source_range,
                task_scope=summary.task_scope,
                dependencies=summary.dependencies,
                metrics={
                    "usage_count": 0,
                    "failure_count": 0,
                    "success_delta": None,
                    "last_verified": None,
                    "average_cost_saved": None,
                },
                status=SkillStatus.draft,
            )
            record = SkillRecord(
                name=spec.name,
                version=spec.version,
                status=spec.status.value,
                spec=spec.model_dump(mode="json"),
                source_run_id=run_id,
            )
            session.add(record)
            _record_skill_event(
                session,
                audit_run_id or run_id,
                "skill_candidate_created",
                _skill_event_payload(spec),
            )
            session.commit()
            return spec

    async def promote(
        self, candidate_id: int | SkillSpec, audit_run_id: str | None = None
    ) -> SkillSpec:
        """Promote a draft, validated, or staged skill candidate under a database row lock."""

        with self.session_factory() as session:
            record = _record_for_candidate(session, candidate_id, lock=True)
            if record.status == SkillStatus.deprecated.value:
                raise SkillLibraryError("Cannot promote a deprecated skill.")
            spec = _record_to_spec(record).model_copy(update={"status": SkillStatus.promoted})
            record.status = SkillStatus.promoted.value
            record.spec = spec.model_dump(mode="json")
            _record_skill_event(
                session,
                audit_run_id or spec.source_run_id,
                "skill_promoted",
                _skill_event_payload(spec),
            )
            session.commit()
            return spec

    async def deprecate(
        self,
        skill_id: int | str,
        version: str | None = None,
        reason: str = "",
        audit_run_id: str | None = None,
    ) -> SkillSpec:
        """Mark a skill version as deprecated without deleting source trajectory metadata."""

        with self.session_factory() as session:
            record = _record_for_identifier(session, skill_id, version, lock=True)
            spec = _record_to_spec(record)
            metrics = dict(spec.metrics)
            metrics["deprecation_reason"] = reason
            spec = spec.model_copy(update={"status": SkillStatus.deprecated, "metrics": metrics})
            record.status = SkillStatus.deprecated.value
            record.spec = spec.model_dump(mode="json")
            _record_skill_event(
                session,
                audit_run_id or spec.source_run_id,
                "skill_deprecated",
                {**_skill_event_payload(spec), "reason": reason},
            )
            session.commit()
            return spec

    async def export_markdown(
        self,
        skill_id: int | str,
        version: str | None = None,
        export_dir: str | Path | None = None,
        audit_run_id: str | None = None,
    ) -> Path:
        """Export a skill review snapshot to Markdown while keeping SQL as source of truth."""

        with self.session_factory() as session:
            record = _record_for_identifier(session, skill_id, version, lock=False)
            spec = _record_to_spec(record)
            output_dir = Path(export_dir) if export_dir is not None else self.export_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{spec.name}_v{spec.version}.md"
            path.write_text(_skill_markdown(spec), encoding="utf-8")
            _record_skill_event(
                session,
                audit_run_id or spec.source_run_id,
                "skill_exported",
                {**_skill_event_payload(spec), "path": str(path)},
            )
            session.commit()
            return path


def _record_for_candidate(session: Session, candidate: int | SkillSpec, lock: bool) -> SkillRecord:
    """Load a SkillRecord by integer id or SkillSpec identity."""

    if isinstance(candidate, int):
        return _record_for_identifier(session, candidate, None, lock=lock)
    return _record_for_identifier(session, candidate.name, candidate.version, lock=lock)


def _record_for_identifier(
    session: Session, identifier: int | str, version: str | None, lock: bool
) -> SkillRecord:
    """Load a SkillRecord by id or name/version, optionally applying a row lock."""

    if isinstance(identifier, int):
        statement = select(SkillRecord).where(
            SkillRecord.id == identifier,
            SkillRecord.status != SKILL_DELETED_STATUS,
        )
        if lock:
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise SkillLibraryError(f"Skill not found: {identifier}")
        return record

    statement = select(SkillRecord).where(
        SkillRecord.name == identifier,
        SkillRecord.status != SKILL_DELETED_STATUS,
    )
    if version is not None:
        statement = statement.where(SkillRecord.version == version)
    if lock:
        statement = statement.with_for_update()
    records = session.scalars(statement).all()
    if not records:
        raise SkillLibraryError(f"Skill not found: {identifier}:{version or 'latest'}")
    return sorted(records, key=lambda item: _version_key(item.version), reverse=True)[0]


def _record_to_spec(record: SkillRecord) -> SkillSpec:
    """Deserialize a SQL SkillRecord into a validated SkillSpec."""

    return SkillSpec.model_validate(record.spec)


def _successful_progress_steps(steps: list[StepRecord]) -> list[StepRecord]:
    """Return successful steps that can encode reusable task progress."""

    return [
        step
        for step in steps
        if step.action_result.get("ok") is True
        and isinstance(step.action, dict)
        and step.action.get("type") in PROMOTABLE_ACTION_TYPES
    ]


def _search_skill_specs(
    skills: Sequence[SkillSpec],
    query: str,
    scope: SkillSearchScope,
    limit: int,
) -> list[SkillSpec]:
    """Rank an explicit skill collection without consulting mutable storage."""

    return [match.skill for match in _rank_skill_specs(skills, query, scope, limit)]


def _rank_skill_specs(
    skills: Sequence[SkillSpec],
    query: str,
    scope: SkillSearchScope,
    limit: int,
) -> list[SkillSearchMatch]:
    """Rank skills while separating broad lexical rank from semantic relevance."""

    matches: list[SkillSearchMatch] = []
    for skill in skills:
        raw_score = _score_skill(skill, query, scope)
        if raw_score <= 0:
            continue
        relevance, task_matches, priority_matches = _skill_relevance(
            skill,
            query,
            scope,
        )
        matches.append(
            SkillSearchMatch(
                skill=skill,
                raw_score=raw_score,
                relevance=relevance,
                matched_task_terms=task_matches,
                matched_priority_terms=priority_matches,
            )
        )
    return sorted(
        matches,
        key=lambda match: (
            -match.relevance,
            -match.raw_score,
            match.skill.name,
            match.skill.version,
        ),
    )[:limit]


def _score_skill(spec: SkillSpec, query: str, scope: SkillSearchScope) -> int:
    """Score one skill using the Week 7 multi-level retrieval policy."""

    query_tokens = _tokens(query)
    canonical_ids = set(scope.canonical_ids)
    task_tags = set(scope.task_tags)
    allowed_actions = set(scope.allowed_actions)
    action_types = {action.type for action in spec.action_plan}
    trigger_tokens = (
        set().union(*(_tokens(trigger) for trigger in spec.triggers)) if spec.triggers else set()
    )
    scope_tokens = (
        set().union(*(_tokens(item) for item in spec.task_scope)) if spec.task_scope else set()
    )
    dependency_tokens = (
        set().union(*(_tokens(item) for item in spec.dependencies)) if spec.dependencies else set()
    )

    score = 0
    score += 100 * len((query_tokens | canonical_ids) & trigger_tokens)
    score += 50 * len((task_tags | query_tokens) & scope_tokens)
    score += 30 * len(allowed_actions & action_types)
    score += 20 * len((query_tokens | canonical_ids) & dependency_tokens)
    strategy_tokens = _tokens(spec.strategy_summary or "")
    plan_tokens = (
        set().union(
            *(_tokens(json.dumps(item, sort_keys=True)) for item in spec.parameterized_plan)
        )
        if spec.parameterized_plan
        else set()
    )
    score += 5 * len(
        query_tokens
        & (_tokens(spec.name) | _tokens(spec.description) | strategy_tokens | plan_tokens)
    )
    return score


_RELEVANCE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "category",
        "executable",
        "for",
        "from",
        "in",
        "minecraft",
        "minedojo",
        "of",
        "one",
        "programmatic",
        "task",
        "term",
        "the",
        "to",
        "with",
    }
)


def _skill_relevance(
    spec: SkillSpec,
    query: str,
    scope: SkillSearchScope,
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    """Measure task coverage without letting shared action types make a skill relevant."""

    task_source = " ".join(scope.task_terms) if scope.task_terms else query
    task_tokens = _meaningful_tokens(task_source)
    if scope.task_id:
        task_tokens.update(_meaningful_tokens(scope.task_id))
    task_tokens.update(_meaningful_tokens(" ".join(scope.canonical_ids)))
    priority_tokens = _meaningful_tokens(" ".join(scope.priority_terms))
    skill_tokens = _skill_semantic_tokens(spec)
    matched_task = tuple(sorted(task_tokens & skill_tokens))
    matched_priority = tuple(sorted(priority_tokens & skill_tokens))
    task_coverage = len(matched_task) / max(1, len(task_tokens))
    exact_task_match = bool(
        scope.task_id
        and _meaningful_tokens(scope.task_id)
        and _meaningful_tokens(scope.task_id).issubset(skill_tokens)
    )
    relevance = 1.0 if exact_task_match else task_coverage
    if matched_priority:
        # An exact current blocker such as no_path is a valid contextual trigger
        # even when the reusable recovery skill is intentionally task-agnostic.
        relevance = max(relevance, 0.75)
    return round(min(1.0, relevance), 6), matched_task, matched_priority


def _skill_semantic_tokens(spec: SkillSpec) -> set[str]:
    """Return semantic Skill terms while excluding shared action compatibility."""

    fields = [
        spec.name,
        spec.description,
        spec.strategy_summary or "",
        *spec.triggers,
        *spec.task_scope,
        *spec.dependencies,
        *(json.dumps(item, sort_keys=True) for item in spec.parameterized_plan),
    ]
    return _meaningful_tokens(" ".join(fields))


def _meaningful_tokens(text: str) -> set[str]:
    """Remove namespace boilerplate and prose stopwords from lexical anchors."""

    return _tokens(text) - _RELEVANCE_STOPWORDS


def _coerce_scope(scope: SkillSearchScope | dict[str, Any] | None) -> SkillSearchScope:
    """Normalize optional search scope input."""

    if scope is None:
        return SkillSearchScope()
    if isinstance(scope, SkillSearchScope):
        return scope
    return SkillSearchScope(
        task_id=scope.get("task_id"),
        task_tags=tuple(str(item) for item in scope.get("task_tags", [])),
        canonical_ids=tuple(str(item) for item in scope.get("canonical_ids", [])),
        allowed_actions=tuple(str(item) for item in scope.get("allowed_actions", [])),
        task_terms=tuple(str(item) for item in scope.get("task_terms", [])),
        priority_terms=tuple(str(item) for item in scope.get("priority_terms", [])),
    )


def _scope_payload(scope: SkillSearchScope) -> dict[str, Any]:
    """Convert a search scope to JSON-safe audit payload."""

    return {
        "task_id": scope.task_id,
        "task_tags": list(scope.task_tags),
        "canonical_ids": list(scope.canonical_ids),
        "allowed_actions": list(scope.allowed_actions),
        "task_terms": list(scope.task_terms),
        "priority_terms": list(scope.priority_terms),
    }


def _next_version(session: Session, name: str) -> str:
    """Return the next patch version for one skill name."""

    versions = [
        record.version
        for record in session.scalars(select(SkillRecord).where(SkillRecord.name == name))
    ]
    if not versions:
        return "0.1.0"
    major, minor, patch = max((_version_key(version) for version in versions), default=(0, 1, -1))
    return f"{major}.{minor}.{patch + 1}"


def _version_key(version: str) -> tuple[int, int, int]:
    """Parse semantic-ish versions into sortable tuples."""

    parts = [int(part) for part in re.findall(r"\d+", version)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def _candidate_description(run: RunRecord, action_plan: list[HarnessAction]) -> str:
    """Build a concise human-review description for a generated skill candidate."""

    goal = run.task_spec.get("goal") if isinstance(run.task_spec, dict) else None
    action_types = ", ".join(action.type for action in action_plan)
    return f"Skill candidate from run {run.id} for task {run.task_id}. Goal: {goal or 'unknown'}. Actions: {action_types}."


def _candidate_triggers(run: RunRecord, action_plan: list[HarnessAction]) -> list[str]:
    """Extract trigger strings from task metadata and action arguments."""

    triggers: set[str] = {_slug(run.task_id), run.task_id}
    if isinstance(run.task_spec, dict):
        for key in ("goal", "category", "family"):
            value = run.task_spec.get(key)
            if isinstance(value, str):
                triggers.update(_tokens(value))
        for tag in run.task_spec.get("knowledge_tags", []):
            if isinstance(tag, str):
                triggers.add(tag)
                triggers.update(_tokens(tag))
    for action in action_plan:
        triggers.add(str(action.type))
        for value in action.args.values():
            if isinstance(value, str):
                triggers.add(value)
                triggers.update(_tokens(value))
    return sorted(item for item in triggers if item)


def _candidate_preconditions(steps: list[StepRecord]) -> list[str]:
    """Summarize preconditions visible in the first successful step observation."""

    observation = steps[0].observation if steps else {}
    preconditions: set[str] = set()
    for block in observation.get("nearby_blocks", []):
        if isinstance(block, dict) and block.get("name"):
            preconditions.add(f"nearby_block:{block['name']}")
    for item in observation.get("inventory", []):
        if isinstance(item, dict) and item.get("name"):
            preconditions.add(f"inventory:{item['name']}")
    return sorted(preconditions)


def _candidate_task_scope(run: RunRecord) -> list[str]:
    """Build scope tags for task-aware skill retrieval."""

    scope: set[str] = {run.task_id}
    if isinstance(run.task_spec, dict):
        for key in ("category", "family"):
            value = run.task_spec.get(key)
            if isinstance(value, str):
                scope.add(value)
        for action in run.task_spec.get("allowed_actions", []):
            scope.add(f"action:{action}")
        for tag in run.task_spec.get("knowledge_tags", []):
            if isinstance(tag, str):
                scope.add(tag)
    return sorted(scope)


def _candidate_dependencies(action_plan: list[HarnessAction]) -> list[str]:
    """Extract item, block, station, and entity dependencies from action arguments."""

    dependencies: set[str] = set()
    for action in action_plan:
        dependencies.add(f"action:{action.type}")
        for key in ("item", "item_id", "block", "block_id", "entity", "entity_id", "station"):
            value = action.args.get(key)
            if isinstance(value, str) and value:
                dependencies.add(value)
    return sorted(dependencies)


def _record_skill_event(
    session: Session,
    run_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Persist a skill lifecycle event when a run context is available."""

    if run_id is None:
        return
    run = session.get(RunRecord, run_id)
    if run is None:
        return
    run_identity = identity_from_task_spec(run.task_spec)
    event_payload, identity = enrich_event_payload(
        payload,
        AuditIdentity(
            task_id=run.task_id,
            agent_id=run_identity.agent_id,
            worker_id=run_identity.worker_id,
        ),
    )
    session.add(
        TrajectoryEventRecord(
            run_id=run_id,
            event_type=event_type,
            payload=event_payload,
            task_id=identity.task_id,
            agent_id=identity.agent_id,
        )
    )


def _skill_event_payload(spec: SkillSpec) -> dict[str, Any]:
    """Build common JSON-safe audit metadata for skill lifecycle events."""

    return {
        "name": spec.name,
        "version": spec.version,
        "status": spec.status.value,
        "source_run_id": spec.source_run_id,
        "source_step_range": spec.source_step_range.model_dump()
        if spec.source_step_range
        else None,
    }


def _learning_candidate_payload(candidate: LearningCandidateSpec) -> dict[str, Any]:
    """Embed bounded, verifier-backed learning evidence in a contextual skill."""

    return {
        "id": candidate.id,
        "signature": candidate.signature,
        "scope_key": candidate.scope_key,
        "status": candidate.status.value,
        "hypothesis": candidate.hypothesis,
        "failure_status": candidate.failure_status,
        "action_type": candidate.action_type,
        "target": candidate.target,
        "support_count": candidate.support_count,
        "recovery_count": candidate.recovery_count,
        "source_run_ids": candidate.source_run_ids,
        "recovery_run_ids": candidate.recovery_run_ids,
        "knowledge_refs": candidate.knowledge_refs,
    }


def _skill_markdown(spec: SkillSpec) -> str:
    """Render a human-review Markdown snapshot for one skill spec."""

    return "\n".join(
        [
            f"# {spec.name} v{spec.version}",
            "",
            f"- Status: `{spec.status.value}`",
            f"- Source run: `{spec.source_run_id}`",
            f"- Source steps: `{spec.source_step_range.start}-{spec.source_step_range.end}`"
            if spec.source_step_range
            else "- Source steps: `unknown`",
            "",
            "## Description",
            "",
            spec.description,
            "",
            "## Strategy Summary",
            "",
            spec.strategy_summary or "",
            "",
            "## Parameterized Plan",
            "",
            "```json",
            json.dumps(spec.parameterized_plan, indent=2, sort_keys=True),
            "```",
            "",
            "## Recovery Policy",
            "",
            "\n".join(f"- {note}" for note in spec.recovery_policy) or "- None",
            "",
            "## Triggers",
            "",
            "\n".join(f"- `{trigger}`" for trigger in spec.triggers) or "- None",
            "",
            "## Preconditions",
            "",
            "\n".join(f"- `{precondition}`" for precondition in spec.preconditions) or "- None",
            "",
            "## Dependencies",
            "",
            "\n".join(f"- `{dependency}`" for dependency in spec.dependencies) or "- None",
            "",
            "## Action Plan",
            "",
            "```json",
            json.dumps(
                [action.model_dump(mode="json") for action in spec.action_plan],
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
            "## Validation",
            "",
            "```json",
            json.dumps(spec.validation, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def _tokens(text: str) -> set[str]:
    """Tokenize identifiers and prose for deterministic lexical matching."""

    return {token for token in re.split(r"[^a-zA-Z0-9_]+", text.lower()) if token}


def _slug(text: str) -> str:
    """Normalize text into a stable lower snake-case identifier."""

    return (
        "_".join(sorted(_tokens(text)))
        if " " in text
        else re.sub(r"[^a-zA-Z0-9_]+", "_", text.lower()).strip("_")
    )
