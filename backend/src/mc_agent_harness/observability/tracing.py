from __future__ import annotations

from hashlib import sha256
from typing import Any


TRACE_ID_HEX_LENGTH = 32
SPAN_ID_HEX_LENGTH = 16
_TRACE_DOMAIN = "mc-agent-harness.trace.v1"
_ROOT_SPAN_DOMAIN = "mc-agent-harness.run-root-span.v1"
_ROUND_SPAN_DOMAIN = "mc-agent-harness.round-span.v1"


def trace_id_for_run(run_id: str) -> str:
    """Return a stable W3C-compatible trace id for one persisted run."""

    normalized = run_id.strip()
    if not normalized:
        raise ValueError("run_id must not be empty")
    return _stable_hex(_TRACE_DOMAIN, normalized, length=TRACE_ID_HEX_LENGTH)


def span_id_for_round(run_id: str, step_index: int) -> str:
    """Return a stable W3C-compatible span id for one run round."""

    normalized = run_id.strip()
    if not normalized:
        raise ValueError("run_id must not be empty")
    if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
        raise ValueError("step_index must be a non-negative integer")
    return _stable_hex(
        _ROUND_SPAN_DOMAIN,
        normalized,
        str(step_index),
        length=SPAN_ID_HEX_LENGTH,
    )


def root_span_id_for_run(run_id: str) -> str:
    """Return the stable root span id used by run-level audit events."""

    normalized = run_id.strip()
    if not normalized:
        raise ValueError("run_id must not be empty")
    return _stable_hex(_ROOT_SPAN_DOMAIN, normalized, length=SPAN_ID_HEX_LENGTH)


def step_index_from_payload(payload: dict[str, Any]) -> int | None:
    """Extract a valid round index from one trajectory payload."""

    value = payload.get("step_index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def trace_context_for_event(run_id: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Return the trace id and owning root or round span id for an event payload."""

    trace_id = trace_id_for_run(run_id)
    step_index = step_index_from_payload(payload)
    span_id = (
        span_id_for_round(run_id, step_index)
        if step_index is not None
        else root_span_id_for_run(run_id)
    )
    return trace_id, span_id


def enrich_trace_payload(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach stable trace context without mutating the caller's payload."""

    trace_id, span_id = trace_context_for_event(run_id, payload)
    return {**payload, "trace_id": trace_id, "span_id": span_id}


def _stable_hex(domain: str, *parts: str, length: int) -> str:
    """Hash a domain-separated identity into a non-zero lowercase hex id."""

    digest = sha256("\0".join((domain, *parts)).encode("utf-8")).hexdigest()[:length]
    if set(digest) == {"0"}:
        return f"{'0' * (length - 1)}1"
    return digest
