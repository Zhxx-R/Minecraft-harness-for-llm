from typing import Any, Literal

from pydantic import BaseModel, Field


ActionType = Literal[
    "resolve_terms",
    "get_recipe",
    "retrieve_docs",
    "scan_blocks",
    "scan_entities",
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
    "fight_entity",
    "use_item",
    "consume_item",
    "move_to_and_engage_combat",
    "engage_combat",
    "query_inventory",
    "execute_skill",
    "request_visual_snapshot",
    "submit_for_evaluation",
]


class HarnessAction(BaseModel):
    """Validated high-level action passed from the harness to the runtime worker."""

    type: ActionType
    args: dict[str, Any] = Field(default_factory=dict)


class KnowledgeNeed(BaseModel):
    """Auditable model claim about whether one decision needs knowledge lookup."""

    needed: bool = False
    query: str | None = None
    reason: str | None = None


class MemoryUpdate(BaseModel):
    """One source-grounded run-memory write requested alongside the next action."""

    memory_key: str | None = Field(default=None, min_length=1, max_length=160)
    source_ref: str = Field(min_length=1, max_length=200)
    paths: list[str] = Field(min_length=1, max_length=8)
    note: str = Field(default="", max_length=500)


class ActionDecision(BaseModel):
    """Model-facing decision envelope containing an action plus audit rationale."""

    reasoning_summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    knowledge_need: KnowledgeNeed = Field(default_factory=KnowledgeNeed)
    memory_update: list[MemoryUpdate] = Field(default_factory=list, max_length=4)
    action: HarnessAction
