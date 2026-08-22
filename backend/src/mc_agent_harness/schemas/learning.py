from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class LearningCandidateStatus(StrEnum):
    """Lifecycle states separating observed failures from validated reusable lessons."""

    observed = "observed"
    hypothesized = "hypothesized"
    corroborated = "corroborated"
    validated = "validated"
    promoted = "promoted"
    rejected = "rejected"
    expired = "expired"


class LearningCandidateKind(StrEnum):
    """Stable classes used to retrieve and review failure-derived learning."""

    navigation_recovery = "navigation_recovery"
    combat_adaptation = "combat_adaptation"
    resource_strategy = "resource_strategy"
    processing_recovery = "processing_recovery"
    tactical_recovery = "tactical_recovery"


class LearningCandidateSpec(BaseModel):
    """Portable, auditable hypothesis that is not yet an executable or promoted skill."""

    id: int | None = None
    signature: str
    scope_key: str
    kind: LearningCandidateKind
    status: LearningCandidateStatus
    hypothesis: str
    failure_status: str
    action_type: str
    target: str | None = None
    support_count: int = 1
    recovery_count: int = 0
    contradiction_count: int = 0
    confidence: float = 0.2
    evidence: dict[str, Any] = Field(default_factory=dict)
    knowledge_refs: list[dict[str, Any]] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    recovery_run_ids: list[str] = Field(default_factory=list)
