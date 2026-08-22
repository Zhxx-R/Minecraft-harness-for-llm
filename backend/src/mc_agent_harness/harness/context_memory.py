from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from mc_agent_harness.harness.state_summary import compress_action_evidence
from mc_agent_harness.schemas.action import HarnessAction, MemoryUpdate


KNOWLEDGE_ACTION_TYPES = frozenset({"resolve_terms", "get_recipe", "retrieve_docs"})
_MEMORY_SOURCE_REF = re.compile(
    r"^step:(?P<step>\d+)/(?P<scope>action_result|[a-z0-9_]+)"
    r"(?:/entity:(?P<entity_id>[^/]+))?$"
)
_MISSING = object()
_MAX_MEMORY_SOURCES = 16


@dataclass(slots=True)
class MemorySource:
    """One raw audited action result addressable by later memory updates."""

    step_index: int
    action_type: str
    action_result: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "action_type": self.action_type,
            "action_result": copy.deepcopy(self.action_result),
        }


@dataclass(slots=True)
class AgentMemoryEntry:
    """One model-selected note whose facts were resolved from an audited source."""

    memory_key: str
    source_ref: str
    source_step_index: int
    source_action_type: str
    selected_values: list[dict[str, Any]]
    note: str
    created_at_step: int
    updated_at_step: int

    def to_json(self) -> dict[str, Any]:
        return {
            "memory_key": self.memory_key,
            "source_ref": self.source_ref,
            "source_step_index": self.source_step_index,
            "source_action_type": self.source_action_type,
            "selected_values": copy.deepcopy(self.selected_values),
            "note": self.note,
            "created_at_step": self.created_at_step,
            "updated_at_step": self.updated_at_step,
            "evidence_policy": "json_pointer_values_resolved_by_harness",
        }


@dataclass(slots=True)
class RunAgentMemory:
    """Run-scoped, model-selected memory backed by immutable action-result sources."""

    entries: dict[str, AgentMemoryEntry] = field(default_factory=dict)
    sources: dict[int, MemorySource] = field(default_factory=dict)

    def record_source(
        self,
        *,
        step_index: int,
        action: HarnessAction,
        result: dict[str, Any],
    ) -> None:
        """Retain a bounded raw source index for subsequent pointer resolution."""

        self.sources[step_index] = MemorySource(
            step_index=step_index,
            action_type=action.type,
            action_result=copy.deepcopy(result),
        )
        for old_step in sorted(self.sources)[:-_MAX_MEMORY_SOURCES]:
            self.sources.pop(old_step, None)

    def apply_updates(
        self,
        updates: list[MemoryUpdate],
        *,
        decision_step_index: int,
    ) -> list[dict[str, Any]]:
        """Resolve requested pointers atomically per update and retain verified values."""

        outcomes: list[dict[str, Any]] = []
        for update in updates:
            outcome = self._apply_one(update, decision_step_index=decision_step_index)
            outcomes.append(outcome)
        return outcomes

    def _apply_one(
        self,
        update: MemoryUpdate,
        *,
        decision_step_index: int,
    ) -> dict[str, Any]:
        source_match = _MEMORY_SOURCE_REF.fullmatch(update.source_ref)
        if source_match is None:
            return _memory_rejection(
                update,
                "invalid_source_ref",
                (
                    "source_ref must be step:N/action_result or "
                    "step:N/<action_type>/entity:<entity_id>."
                ),
            )
        source_step = int(source_match.group("step"))
        source = self.sources.get(source_step)
        if source is None:
            return _memory_rejection(
                update,
                "source_not_available",
                f"No retained action result exists for step {source_step}.",
            )
        scope = source_match.group("scope")
        if scope != "action_result" and scope != source.action_type:
            return _memory_rejection(
                update,
                "source_action_mismatch",
                f"Step {source_step} was {source.action_type}, not {scope}.",
            )
        source_value: Any = source.action_result
        entity_id = source_match.group("entity_id")
        if entity_id is not None:
            source_value = _find_entity_by_id(source_value, entity_id)
            if source_value is _MISSING:
                return _memory_rejection(
                    update,
                    "source_entity_not_found",
                    f"Entity {entity_id} was not present in step {source_step}.",
                )

        selected_values: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for path in update.paths:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            value = _resolve_json_pointer(source_value, path)
            if value is _MISSING:
                return _memory_rejection(
                    update,
                    "path_not_found",
                    f"JSON Pointer {path!r} does not exist under {update.source_ref}.",
                )
            selected_values.append({"path": path, "value": copy.deepcopy(value)})
        if not selected_values:
            return _memory_rejection(
                update,
                "no_unique_paths",
                "memory_update did not contain a unique resolvable path.",
            )
        if _json_size(selected_values) > 6000:
            return _memory_rejection(
                update,
                "selected_values_too_large",
                "Selected source values exceed the bounded memory-entry size.",
            )

        memory_key = update.memory_key or _derived_memory_key(
            update.source_ref,
            [row["path"] for row in selected_values],
        )
        existing = self.entries.get(memory_key)
        entry = AgentMemoryEntry(
            memory_key=memory_key,
            source_ref=update.source_ref,
            source_step_index=source.step_index,
            source_action_type=source.action_type,
            selected_values=selected_values,
            note=update.note,
            created_at_step=(
                existing.created_at_step if existing is not None else decision_step_index
            ),
            updated_at_step=decision_step_index,
        )
        self.entries[memory_key] = entry
        return {
            "accepted": True,
            "operation": "updated" if existing is not None else "created",
            "entry": entry.to_json(),
        }

    def context_payload(self, *, max_chars: int) -> dict[str, Any]:
        """Project durable selected facts without exposing the raw source archive."""

        ordered = sorted(
            self.entries.values(),
            key=lambda entry: (entry.updated_at_step, entry.memory_key),
            reverse=True,
        )
        if max_chars <= 0:
            return {
                "entries": [],
                "omitted_count": len(ordered),
                "compression": "evicted_agent_memory",
            }
        entries: list[dict[str, Any]] = []
        for entry in ordered:
            candidate = entry.to_json()
            if _json_size({"entries": [*entries, candidate]}) > max_chars:
                continue
            entries.append(candidate)
        return {
            "entries": entries,
            "omitted_count": len(ordered) - len(entries),
            "compression": "full" if len(entries) == len(ordered) else "bounded",
        }

    def to_json(self) -> dict[str, Any]:
        """Serialize entries and the bounded source index for checkpoint recovery."""

        return {
            "entries": [
                entry.to_json()
                for entry in sorted(
                    self.entries.values(),
                    key=lambda item: item.memory_key,
                )
            ],
            "sources": [
                self.sources[step_index].to_json()
                for step_index in sorted(self.sources)
            ],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> RunAgentMemory:
        memory = cls()
        data = payload if isinstance(payload, dict) else {}
        for row in data.get("entries", []):
            if not isinstance(row, dict) or not row.get("memory_key"):
                continue
            entry = AgentMemoryEntry(
                memory_key=str(row["memory_key"]),
                source_ref=str(row.get("source_ref") or ""),
                source_step_index=int(row.get("source_step_index", 0)),
                source_action_type=str(row.get("source_action_type") or "unknown"),
                selected_values=[
                    dict(value)
                    for value in row.get("selected_values", [])
                    if isinstance(value, dict)
                ],
                note=str(row.get("note") or ""),
                created_at_step=int(row.get("created_at_step", 0)),
                updated_at_step=int(row.get("updated_at_step", 0)),
            )
            memory.entries[entry.memory_key] = entry
        for row in data.get("sources", [])[-_MAX_MEMORY_SOURCES:]:
            if not isinstance(row, dict) or not isinstance(row.get("action_result"), dict):
                continue
            source = MemorySource(
                step_index=int(row.get("step_index", 0)),
                action_type=str(row.get("action_type") or "unknown"),
                action_result=copy.deepcopy(row["action_result"]),
            )
            memory.sources[source.step_index] = source
        return memory


@dataclass(slots=True)
class KnowledgeLedgerEntry:
    """One repeatable knowledge result retained with its source step and cache identity."""

    signature: str
    action_type: str
    args: dict[str, Any]
    result: dict[str, Any]
    summary: str
    first_step_index: int
    last_step_index: int
    access_count: int = 1

    def to_json(
        self,
        *,
        include_result: bool = True,
        compact_result: bool = True,
    ) -> dict[str, Any]:
        """Serialize one entry with optional low-priority result details."""

        payload = {
            "signature": self.signature,
            "action_type": self.action_type,
            "args": self.args,
            "summary": self.summary,
            "first_step_index": self.first_step_index,
            "last_step_index": self.last_step_index,
            "access_count": self.access_count,
            "requery_allowed": True,
        }
        if include_result:
            payload["result"] = (
                _knowledge_context_result(self.result)
                if compact_result
                else copy.deepcopy(self.result)
            )
        return payload


@dataclass(slots=True)
class RunKnowledgeLedger:
    """Run-scoped exact knowledge cache whose prompt projection is safe to evict."""

    entries: dict[str, KnowledgeLedgerEntry] = field(default_factory=dict)

    def cached_result(
        self,
        action: HarnessAction,
        *,
        step_index: int,
        knowledge_revision: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an audited cache hit for an identical deterministic knowledge action."""

        if action.type not in KNOWLEDGE_ACTION_TYPES:
            return None
        signature = knowledge_signature(action, knowledge_revision=knowledge_revision)
        entry = self.entries.get(signature)
        if entry is None:
            return None
        entry.last_step_index = step_index
        entry.access_count += 1
        return {
            **copy.deepcopy(entry.result),
            "cache_hit": True,
            "cache_signature": signature,
            "cached_from_step_index": entry.first_step_index,
        }

    def record(
        self,
        *,
        step_index: int,
        action: HarnessAction,
        result: dict[str, Any],
        observation: dict[str, Any],
        task_spec: dict[str, Any],
        knowledge_revision: str | None = None,
    ) -> None:
        """Store the first result for one exact query and update later access metadata."""

        if action.type not in KNOWLEDGE_ACTION_TYPES:
            return
        signature = knowledge_signature(action, knowledge_revision=knowledge_revision)
        for stale_signature, stale_entry in list(self.entries.items()):
            if (
                stale_signature != signature
                and stale_entry.action_type == action.type
                and stale_entry.args == action.args
            ):
                # The immutable trajectory retains the old tool event. The live
                # exact-query ledger keeps only the current corpus revision so a
                # stale fact cannot remain beside its replacement in the prompt.
                self.entries.pop(stale_signature, None)
        existing = self.entries.get(signature)
        if existing is not None:
            existing.last_step_index = step_index
            if not result.get("cache_hit"):
                existing.result = copy.deepcopy(result)
                existing.summary = _knowledge_summary(
                    step_index=step_index,
                    action=action,
                    result=result,
                    observation=observation,
                    task_spec=task_spec,
                )
            return
        self.entries[signature] = KnowledgeLedgerEntry(
            signature=signature,
            action_type=action.type,
            args=copy.deepcopy(action.args),
            result=copy.deepcopy(result),
            summary=_knowledge_summary(
                step_index=step_index,
                action=action,
                result=result,
                observation=observation,
                task_spec=task_spec,
            ),
            first_step_index=step_index,
            last_step_index=step_index,
        )

    def context_payload(
        self,
        *,
        max_chars: int,
        include_details: bool,
        exclude_signatures: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Project low-priority knowledge into prompt context and evict it under pressure."""

        ordered = sorted(
            (
                entry
                for signature, entry in self.entries.items()
                if signature not in exclude_signatures
            ),
            key=lambda entry: (entry.last_step_index, entry.signature),
            reverse=True,
        )
        if max_chars <= 0:
            return {
                "entries": [],
                "omitted_count": len(ordered),
                "requery_allowed": True,
                "compression": "evicted_repeatable_knowledge",
            }
        entries: list[dict[str, Any]] = []
        summarized_count = 0
        for entry in ordered:
            candidate = entry.to_json(include_result=include_details)
            was_summarized = False
            if include_details and _json_size({"entries": [*entries, candidate]}) > max_chars:
                candidate = entry.to_json(include_result=False)
                was_summarized = True
            if _json_size({"entries": [*entries, candidate]}) > max_chars:
                continue
            entries.append(candidate)
            if was_summarized:
                summarized_count += 1
        if ordered and not entries:
            compression = "evicted_repeatable_knowledge"
        elif include_details and summarized_count == len(entries) and entries:
            compression = "summary_only"
        elif include_details and summarized_count:
            compression = "mixed_full_and_summary"
        elif include_details:
            compression = "full"
        else:
            compression = "summary_only"
        return {
            "entries": entries,
            "omitted_count": len(ordered) - len(entries),
            "requery_allowed": True,
            "compression": compression,
        }

    def to_json(self) -> dict[str, Any]:
        """Serialize the complete compact ledger for checkpoint recovery, not model prompting."""

        return {
            "entries": [
                entry.to_json(include_result=True, compact_result=False)
                for entry in sorted(self.entries.values(), key=lambda item: item.signature)
            ]
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> RunKnowledgeLedger:
        """Restore a ledger from a checkpoint payload."""

        ledger = cls()
        rows = payload.get("entries", []) if isinstance(payload, dict) else []
        for row in rows:
            if not isinstance(row, dict) or not row.get("signature"):
                continue
            entry = KnowledgeLedgerEntry(
                signature=str(row["signature"]),
                action_type=str(row.get("action_type") or "unknown"),
                args=dict(row.get("args") or {}),
                result=dict(row.get("result") or {}),
                summary=str(row.get("summary") or ""),
                first_step_index=int(row.get("first_step_index", 0)),
                last_step_index=int(row.get("last_step_index", 0)),
                access_count=int(row.get("access_count", 1)),
            )
            ledger.entries[entry.signature] = entry
        return ledger


@dataclass(slots=True)
class SkillLedgerEntry:
    """One skill summary retained after its first injection into a run context."""

    identity: str
    summary: dict[str, Any]
    first_step_index: int
    last_step_index: int
    injection_count: int = 1

    def to_json(self, *, compact: bool = False) -> dict[str, Any]:
        """Serialize one skill with injection provenance and optional detail reduction."""

        if compact:
            retained_keys = {
                "name",
                "version",
                "description",
                "strategy_summary",
                "semantics",
                "triggers",
                "preconditions",
                "status",
            }
            payload = {
                key: copy.deepcopy(value)
                for key, value in self.summary.items()
                if key in retained_keys
            }
        else:
            payload = copy.deepcopy(self.summary)
        payload.update(
            {
                "identity": self.identity,
                "first_injected_step_index": self.first_step_index,
                "last_injected_step_index": self.last_step_index,
                "injection_count": self.injection_count,
            }
        )
        return payload


@dataclass(slots=True)
class RunSkillLedger:
    """Run-scoped skill context that prevents duplicate injection across model turns."""

    entries: dict[str, SkillLedgerEntry] = field(default_factory=dict)

    def record(
        self,
        *,
        summary: dict[str, Any],
        step_index: int,
    ) -> None:
        """Remember one injected skill so later turns can reuse one canonical copy."""

        identity = skill_identity(summary.get("name"), summary.get("version"))
        if identity is None:
            return
        existing = self.entries.get(identity)
        if existing is not None:
            existing.summary = copy.deepcopy(summary)
            existing.last_step_index = step_index
            existing.injection_count += 1
            return
        self.entries[identity] = SkillLedgerEntry(
            identity=identity,
            summary=copy.deepcopy(summary),
            first_step_index=step_index,
            last_step_index=step_index,
        )

    def context_payload(self, *, max_chars: int) -> dict[str, Any]:
        """Project current skills once, dropping detail before omitting old entries."""

        ordered = sorted(
            self.entries.values(),
            key=lambda entry: (entry.last_step_index, entry.identity),
            reverse=True,
        )
        if max_chars <= 0:
            return {
                "entries": [],
                "omitted_count": len(ordered),
                "compression": "evicted_skill_context",
            }
        entries: list[dict[str, Any]] = []
        compacted_count = 0
        for entry in ordered:
            candidate = entry.to_json()
            was_compacted = False
            if _json_size({"entries": [*entries, candidate]}) > max_chars:
                candidate = entry.to_json(compact=True)
                was_compacted = True
            if _json_size({"entries": [*entries, candidate]}) > max_chars:
                continue
            entries.append(candidate)
            if was_compacted:
                compacted_count += 1
        if ordered and not entries:
            compression = "evicted_skill_context"
        elif compacted_count == len(entries) and entries:
            compression = "summary_only"
        elif compacted_count:
            compression = "mixed_full_and_summary"
        else:
            compression = "full"
        return {
            "entries": entries,
            "omitted_count": len(ordered) - len(entries),
            "compression": compression,
        }

    def to_json(self) -> dict[str, Any]:
        """Serialize the full ledger for checkpoint recovery."""

        return {
            "entries": [
                entry.to_json()
                for entry in sorted(self.entries.values(), key=lambda item: item.identity)
            ]
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> RunSkillLedger:
        """Restore previously injected skill identities and summaries."""

        ledger = cls()
        rows = payload.get("entries", []) if isinstance(payload, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = str(row.get("identity") or "")
            if not identity:
                identity = skill_identity(row.get("name"), row.get("version")) or ""
            if not identity:
                continue
            summary = {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key
                not in {
                    "identity",
                    "first_injected_step_index",
                    "last_injected_step_index",
                    "injection_count",
                }
            }
            ledger.entries[identity] = SkillLedgerEntry(
                identity=identity,
                summary=summary,
                first_step_index=int(row.get("first_injected_step_index", 0)),
                last_step_index=int(row.get("last_injected_step_index", 0)),
                injection_count=int(row.get("injection_count", 1)),
            )
        return ledger


@dataclass(slots=True)
class TrajectoryContextCompressor:
    """Hierarchical prompt projection over compact action-specific trace entries."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    recent_step_count: int = 2
    max_segment_steps: int = 6

    def record(
        self,
        *,
        step_index: int,
        action: HarnessAction,
        result: dict[str, Any],
        observation: dict[str, Any],
        task_spec: dict[str, Any],
    ) -> None:
        """Append one typed compact entry without retaining the raw worker observation."""

        # Knowledge is deterministic, repeatable input and therefore belongs only in the
        # lower-priority knowledge ledger. It remains available as previous-step evidence
        # for the immediately following ReAct turn.
        if action.type in KNOWLEDGE_ACTION_TYPES:
            return

        evidence = compress_action_evidence(
            step_index=step_index,
            action=action.model_dump(mode="json"),
            action_result=result,
            observation=observation,
            task_spec=task_spec,
        )
        evidence["phase"] = _action_phase(action.type)
        self.entries.append(evidence)

    def context_payload(
        self,
        *,
        max_chars: int,
        exclude_latest: bool = True,
        latest_step_index: int | None = None,
    ) -> dict[str, Any]:
        """Keep recent typed evidence and progressively merge older semantic action phases."""

        latest_is_previous_step = bool(
            exclude_latest
            and self.entries
            and (
                latest_step_index is None
                or self.entries[-1].get("step_index") == latest_step_index
            )
        )
        visible_entries = self.entries[:-1] if latest_is_previous_step else self.entries
        recent = [copy.deepcopy(entry) for entry in visible_entries[-self.recent_step_count :]]
        older = visible_entries[: max(0, len(visible_entries) - self.recent_step_count)]
        segments = _semantic_segments(older, self.max_segment_steps)
        payload = {
            "compression": "hierarchical",
            "total_steps": len(self.entries),
            "latest_step_is_in_compact_evidence": latest_is_previous_step,
            "segments": segments,
            "recent_steps": recent,
        }
        if _json_size(payload) <= max_chars:
            return payload

        slim_segments = [_slim_segment(segment) for segment in segments]
        payload = {
            "compression": "aggressive",
            "total_steps": len(self.entries),
            "latest_step_is_in_compact_evidence": latest_is_previous_step,
            "segments": slim_segments,
            "recent_steps": recent[-1:],
        }
        if _json_size(payload) <= max_chars:
            return payload

        return {
            "compression": "episode",
            "total_steps": len(self.entries),
            "latest_step_is_in_compact_evidence": latest_is_previous_step,
            "episode_summary": _episode_summary(visible_entries),
            "recent_steps": recent[-1:],
        }

    def to_json(self) -> dict[str, Any]:
        """Serialize compact entries for checkpoint recovery."""

        return {"entries": copy.deepcopy(self.entries)}

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> TrajectoryContextCompressor:
        """Restore compact trajectory entries from a checkpoint payload."""

        rows = payload.get("entries", []) if isinstance(payload, dict) else []
        return cls(entries=[dict(row) for row in rows if isinstance(row, dict)])


@dataclass(slots=True)
class RunContextMemory:
    """Run-local prompt memory combining trace, skill, and repeatable knowledge evidence."""

    trajectory: TrajectoryContextCompressor = field(default_factory=TrajectoryContextCompressor)
    knowledge: RunKnowledgeLedger = field(default_factory=RunKnowledgeLedger)
    skills: RunSkillLedger = field(default_factory=RunSkillLedger)
    memory: RunAgentMemory = field(default_factory=RunAgentMemory)

    def context_payload(
        self,
        *,
        max_chars: int,
        max_knowledge_chars: int,
        max_skill_chars: int = 4000,
        max_memory_chars: int = 3500,
        exclude_knowledge_signature: str | None = None,
        previous_step_index: int | None = None,
    ) -> dict[str, Any]:
        """Prioritize trajectory and skills, then evict repeatable knowledge under pressure."""

        trajectory_budget = max(
            1000,
            max_chars - max_knowledge_chars - max_skill_chars - max_memory_chars,
        )
        trajectory = self.trajectory.context_payload(
            max_chars=trajectory_budget,
            latest_step_index=previous_step_index,
        )
        compression = str(trajectory.get("compression") or "hierarchical")
        include_knowledge_details = compression == "hierarchical" and len(self.trajectory.entries) <= 8
        knowledge_budget = max_knowledge_chars if include_knowledge_details else max_knowledge_chars // 3
        knowledge = self.knowledge.context_payload(
            max_chars=knowledge_budget,
            include_details=include_knowledge_details,
            exclude_signatures=(
                frozenset({exclude_knowledge_signature})
                if exclude_knowledge_signature is not None
                else frozenset()
            ),
        )
        skills = self.skills.context_payload(max_chars=max_skill_chars)
        memory = self.memory.context_payload(max_chars=max_memory_chars)
        payload = {
            "trajectory": trajectory,
            "memory": memory,
            "skills": skills,
            "knowledge": knowledge,
        }
        if _json_size(payload) <= max_chars:
            return payload
        payload["knowledge"] = self.knowledge.context_payload(
            max_chars=0,
            include_details=False,
            exclude_signatures=(
                frozenset({exclude_knowledge_signature})
                if exclude_knowledge_signature is not None
                else frozenset()
            ),
        )
        if _json_size(payload) <= max_chars:
            return payload
        payload["skills"] = self.skills.context_payload(
            max_chars=max(0, max_skill_chars - (_json_size(payload) - max_chars) - 128)
        )
        return payload

    def to_json(self) -> dict[str, Any]:
        """Serialize run memory for checkpoint persistence."""

        return {
            "trajectory": self.trajectory.to_json(),
            "knowledge": self.knowledge.to_json(),
            "skills": self.skills.to_json(),
            "memory": self.memory.to_json(),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> RunContextMemory:
        """Restore run memory from a checkpoint payload."""

        data = payload if isinstance(payload, dict) else {}
        return cls(
            trajectory=TrajectoryContextCompressor.from_json(data.get("trajectory")),
            knowledge=RunKnowledgeLedger.from_json(data.get("knowledge")),
            skills=RunSkillLedger.from_json(data.get("skills")),
            memory=RunAgentMemory.from_json(data.get("memory")),
        )


def skill_identity(name: Any, version: Any) -> str | None:
    """Build the canonical exact identity used for context-level skill deduplication."""

    normalized_name = str(name or "").strip().casefold()
    normalized_version = str(version or "").strip().casefold()
    if not normalized_name or not normalized_version:
        return None
    return f"{normalized_name}@{normalized_version}"


def knowledge_signature(
    action: HarnessAction,
    *,
    knowledge_revision: str | None = None,
) -> str:
    """Build a stable exact-cache key from a validated knowledge action."""

    return knowledge_signature_for(
        action.type,
        action.args,
        knowledge_revision=knowledge_revision,
    )


def knowledge_signature_for(
    action_type: str,
    args: dict[str, Any],
    *,
    knowledge_revision: str | None = None,
) -> str:
    """Build the same exact-cache key from an audited raw action payload."""

    normalized_args = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    normalized_revision = str(knowledge_revision or "static").strip() or "static"
    return f"{normalized_revision}:{action_type}:{normalized_args}"


def _knowledge_summary(
    *,
    step_index: int,
    action: HarnessAction,
    result: dict[str, Any],
    observation: dict[str, Any],
    task_spec: dict[str, Any],
) -> str:
    """Reuse action-specific compression to summarize one repeatable knowledge result."""

    evidence = compress_action_evidence(
        step_index=step_index,
        action=action.model_dump(mode="json"),
        action_result=result,
        observation=observation,
        task_spec=task_spec,
    )
    return str(evidence.get("summary") or result.get("state_summary") or "Knowledge retrieved.")


def _knowledge_context_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep cited knowledge facts while excluding policy and other repeated transport metadata."""

    keys = {
        "docs",
        "item",
        "ok",
        "query",
        "recipe",
        "scope",
        "state_summary",
        "terms",
        "tool",
    }
    return {key: copy.deepcopy(result[key]) for key in keys if key in result}


def _semantic_segments(
    entries: list[dict[str, Any]],
    max_segment_steps: int,
) -> list[dict[str, Any]]:
    """Group consecutive actions by semantic phase and bounded segment size."""

    groups: list[list[dict[str, Any]]] = []
    for entry in entries:
        if (
            not groups
            or groups[-1][-1].get("phase") != entry.get("phase")
            or len(groups[-1]) >= max_segment_steps
        ):
            groups.append([])
        groups[-1].append(entry)
    return [_segment_summary(group) for group in groups if group]


def _segment_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one semantic action phase with provenance and typed progress evidence."""

    action_counts = Counter(str(entry.get("action_type") or "unknown") for entry in entries)
    blockers = _unique_text(
        blocker
        for entry in entries
        for blocker in entry.get("blockers", [])
        if blocker
    )
    progress = _progress_signals(entries)
    return {
        "step_range": {
            "start": entries[0].get("step_index"),
            "end": entries[-1].get("step_index"),
        },
        "phase": entries[0].get("phase"),
        "action_counts": dict(sorted(action_counts.items())),
        "last_summary": entries[-1].get("summary"),
        "progress_signals": progress[-6:],
        "unresolved_blockers": blockers[-6:],
        "source_step_indices": [entry.get("step_index") for entry in entries],
    }


def _slim_segment(segment: dict[str, Any]) -> dict[str, Any]:
    """Remove re-derivable narrative while retaining provenance, progress, and blockers."""

    return {
        "step_range": segment.get("step_range"),
        "phase": segment.get("phase"),
        "action_counts": segment.get("action_counts"),
        "progress_signals": segment.get("progress_signals"),
        "unresolved_blockers": segment.get("unresolved_blockers"),
    }


def _episode_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the smallest deterministic summary used only under severe prompt pressure."""

    counts = Counter(str(entry.get("action_type") or "unknown") for entry in entries)
    failures = [
        {
            "step_index": entry.get("step_index"),
            "action_type": entry.get("action_type"),
            "blockers": entry.get("blockers", []),
        }
        for entry in entries
        if entry.get("ok") is False
    ]
    return {
        "step_range": {
            "start": entries[0].get("step_index") if entries else None,
            "end": entries[-1].get("step_index") if entries else None,
        },
        "action_counts": dict(sorted(counts.items())),
        "progress_signals": _progress_signals(entries)[-8:],
        "recent_failures": failures[-4:],
    }


def _progress_signals(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract inventory, world, combat, and navigation evidence worth retaining across compression."""

    signals: list[dict[str, Any]] = []
    for entry in entries:
        step_index = entry.get("step_index")
        for key in (
            "inventory_delta",
            "world_delta",
            "drop_observation_status",
            "spawned_drops",
            "status",
            "progress_status",
            "nearest_reachable_position",
        ):
            value = entry.get(key)
            if value not in (None, {}, [], ""):
                signals.append({"step_index": step_index, key: value})
    return signals


def _action_phase(action_type: str) -> str:
    """Map one primitive to a stable semantic phase for segment-level compression."""

    if action_type in KNOWLEDGE_ACTION_TYPES:
        return "knowledge"
    if action_type in {"move_to", "scan_blocks", "scan_entities", "scan_dropped_items"}:
        return "navigation"
    if action_type in {"dig_block_at", "wait_ticks", "query_inventory"}:
        return "collection"
    if action_type in {"process_item", "craft_item", "smelt_item", "place_block", "use_item"}:
        return "processing"
    if action_type in {
        "engage_combat",
        "move_to_and_engage_combat",
        "fight_entity",
        "equip_item",
        "consume_item",
    }:
        return "combat"
    return "interaction"


def _unique_text(values: Any) -> list[str]:
    """Return stable unique text values while preserving evidence order."""

    output: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in output:
            output.append(text)
    return output


def _memory_rejection(
    update: MemoryUpdate,
    code: str,
    message: str,
) -> dict[str, Any]:
    """Return an auditable rejection without changing the selected-memory ledger."""

    return {
        "accepted": False,
        "error_code": code,
        "message": message,
        "request": update.model_dump(mode="json"),
    }


def _derived_memory_key(source_ref: str, paths: list[str]) -> str:
    """Build a stable key when the model does not need cross-source replacement."""

    identity = json.dumps(
        {"source_ref": source_ref, "paths": paths},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"source_fact:{digest}"


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    """Resolve RFC 6901 object keys and array indices without evaluating expressions."""

    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return _MISSING
    current = value
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit():
                return _MISSING
            index = int(token)
            if index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
            continue
        return _MISSING
    return current


def _find_entity_by_id(value: Any, entity_id: str) -> Any:
    """Find one entity-shaped object recursively without action-specific field maps."""

    if isinstance(value, dict):
        candidate_id = value.get("entity_id")
        if candidate_id is None:
            candidate_id = value.get("id")
        if candidate_id is not None and str(candidate_id) == entity_id:
            return value
        for nested in value.values():
            match = _find_entity_by_id(nested, entity_id)
            if match is not _MISSING:
                return match
    elif isinstance(value, list):
        for nested in value:
            match = _find_entity_by_id(nested, entity_id)
            if match is not _MISSING:
                return match
    return _MISSING


def _json_size(value: Any) -> int:
    """Estimate prompt cost in characters using the actual JSON projection."""

    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str))
