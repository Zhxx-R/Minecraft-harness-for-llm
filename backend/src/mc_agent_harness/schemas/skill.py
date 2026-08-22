from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from mc_agent_harness.schemas.action import HarnessAction


class SkillStatus(StrEnum):
    """Lifecycle states used to gate skill visibility and reuse."""

    draft = "draft"
    validated = "validated"
    staged = "staged"
    promoted = "promoted"
    deprecated = "deprecated"


class SkillStepRange(BaseModel):
    """Inclusive source step range used to trace a skill back to one trajectory slice."""

    start: int
    end: int


class SkillSpec(BaseModel):
    """Portable skill representation for contextual procedural memory."""

    name: str
    version: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    strategy_summary: str | None = None
    parameterized_plan: list[dict[str, Any]] = Field(default_factory=list)
    recovery_policy: list[str] = Field(default_factory=list)
    source_evidence: dict[str, Any] = Field(default_factory=dict)
    verifier_stats: dict[str, Any] = Field(default_factory=dict)
    action_plan: list[HarnessAction] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    source_run_id: str | None = None
    source_step_range: SkillStepRange | None = None
    task_scope: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    status: SkillStatus = SkillStatus.draft
