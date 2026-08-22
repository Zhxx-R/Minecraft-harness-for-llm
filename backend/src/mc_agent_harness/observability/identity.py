from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuditIdentity:
    """Stable task, agent, and worker identity attached to audit events."""

    task_id: str | None = None
    agent_id: str | None = None
    worker_id: str | None = None


def identity_from_task_spec(task_spec: dict[str, Any] | None) -> AuditIdentity:
    """Extract live agent identity from one persisted task specification."""

    spec = task_spec if isinstance(task_spec, dict) else {}
    runtime = spec.get("runtime") if isinstance(spec.get("runtime"), dict) else {}
    training = spec.get("training") if isinstance(spec.get("training"), dict) else {}
    worker_id = _first_text(training.get("worker_id"))
    agent_id = _first_text(
        spec.get("agent_id"),
        training.get("agent_id"),
        runtime.get("username"),
        worker_id,
    )
    return AuditIdentity(
        task_id=_first_text(spec.get("task_id")),
        agent_id=agent_id,
        worker_id=worker_id,
    )


def resolve_event_identity(
    payload: dict[str, Any],
    fallback: AuditIdentity | None = None,
) -> AuditIdentity:
    """Resolve explicit event identity, then task-spec and recorder defaults."""

    default = fallback or AuditIdentity()
    nested = identity_from_task_spec(
        payload.get("task_spec") if isinstance(payload.get("task_spec"), dict) else None
    )
    worker_id = _first_text(payload.get("worker_id"), nested.worker_id, default.worker_id)
    return AuditIdentity(
        task_id=_first_text(payload.get("task_id"), nested.task_id, default.task_id),
        agent_id=_first_text(
            payload.get("agent_id"),
            payload.get("username"),
            nested.agent_id,
            default.agent_id,
            worker_id,
        ),
        worker_id=worker_id,
    )


def enrich_event_payload(
    payload: dict[str, Any],
    fallback: AuditIdentity | None = None,
) -> tuple[dict[str, Any], AuditIdentity]:
    """Return an event payload with normalized identity fields and its identity."""

    identity = resolve_event_identity(payload, fallback)
    enriched = dict(payload)
    if identity.task_id is not None:
        enriched["task_id"] = identity.task_id
    if identity.agent_id is not None:
        enriched["agent_id"] = identity.agent_id
    if identity.worker_id is not None:
        enriched["worker_id"] = identity.worker_id
    return enriched, identity


def _first_text(*values: Any) -> str | None:
    """Return the first non-empty value as normalized text."""

    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
