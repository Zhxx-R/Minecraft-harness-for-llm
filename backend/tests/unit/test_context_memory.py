from mc_agent_harness.harness.context_memory import RunContextMemory
from mc_agent_harness.schemas.action import HarnessAction, MemoryUpdate


def test_run_knowledge_ledger_caches_exact_query_and_can_be_evicted() -> None:
    """Knowledge remains auditable/cacheable even when its prompt projection is removed."""

    memory = RunContextMemory()
    action = HarnessAction(type="get_recipe", args={"item": "glass"})
    result = {
        "ok": True,
        "tool": "get_recipe",
        "item": "glass",
        "source_policy": "local_only",
        "recipe": {
            "output": "glass",
            "output_count": 1,
            "station": "furnace",
            "ingredients": [{"item_id": "sand", "count": 1}],
        },
    }
    memory.knowledge.record(
        step_index=0,
        action=action,
        result=result,
        observation={},
        task_spec={"goal": "Obtain glass."},
    )

    cached = memory.knowledge.cached_result(action, step_index=3)
    evicted = memory.knowledge.context_payload(max_chars=0, include_details=False)
    restored = RunContextMemory.from_json(memory.to_json())

    assert cached is not None
    assert cached["cache_hit"] is True
    assert cached["recipe"]["station"] == "furnace"
    assert evicted["entries"] == []
    assert evicted["omitted_count"] == 1
    assert evicted["requery_allowed"] is True
    restored_result = restored.knowledge.cached_result(action, step_index=4)
    assert restored_result is not None
    assert restored_result["source_policy"] == "local_only"


def test_run_knowledge_ledger_invalidates_exact_query_when_corpus_revision_changes() -> None:
    memory = RunContextMemory()
    action = HarnessAction(type="retrieve_docs", args={"query": "sheep wool"})
    memory.knowledge.record(
        step_index=0,
        action=action,
        result={"ok": True, "docs": [{"id": "old", "content": "old fact"}]},
        observation={},
        task_spec={"goal": "Find wool."},
        knowledge_revision="knowledge:1",
    )

    assert (
        memory.knowledge.cached_result(
            action,
            step_index=1,
            knowledge_revision="knowledge:1",
        )
        is not None
    )
    assert (
        memory.knowledge.cached_result(
            action,
            step_index=1,
            knowledge_revision="knowledge:2",
        )
        is None
    )

    memory.knowledge.record(
        step_index=1,
        action=action,
        result={"ok": True, "docs": [{"id": "new", "content": "new fact"}]},
        observation={},
        task_spec={"goal": "Find wool."},
        knowledge_revision="knowledge:2",
    )

    assert len(memory.knowledge.entries) == 1
    refreshed = memory.knowledge.cached_result(
        action,
        step_index=2,
        knowledge_revision="knowledge:2",
    )
    assert refreshed is not None
    assert refreshed["docs"][0]["id"] == "new"


def test_large_knowledge_result_falls_back_to_summary_before_eviction() -> None:
    """An oversized document should keep a re-queryable summary when that summary fits."""

    memory = RunContextMemory()
    action = HarnessAction(type="retrieve_docs", args={"query": "enderman behavior"})
    memory.knowledge.record(
        step_index=0,
        action=action,
        result={
            "ok": True,
            "tool": "retrieve_docs",
            "query": "enderman behavior",
            "docs": [
                {
                    "id": "entity/enderman",
                    "title": "Enderman",
                    "content": "teleport " * 2000,
                }
            ],
        },
        observation={},
        task_spec={"goal": "Defeat an enderman."},
    )

    payload = memory.knowledge.context_payload(max_chars=700, include_details=True)

    assert payload["compression"] == "summary_only"
    assert len(payload["entries"]) == 1
    assert "result" not in payload["entries"][0]
    assert payload["entries"][0]["requery_allowed"] is True


def test_trajectory_compression_keeps_typed_recent_evidence_and_skips_knowledge() -> None:
    """Durable history uses action-specific summaries, while repeatable knowledge stays separate."""

    memory = RunContextMemory()
    memory.trajectory.record(
        step_index=0,
        action=HarnessAction(type="get_recipe", args={"item": "glass"}),
        result={"ok": True, "item": "glass", "recipe": {"station": "furnace"}},
        observation={},
        task_spec={"goal": "Obtain glass."},
    )
    for step_index in range(1, 8):
        memory.trajectory.record(
            step_index=step_index,
            action=HarnessAction(type="move_to", args={"position": {"x": step_index, "y": 64, "z": 0}}),
            result={
                "ok": step_index == 7,
                "action_type": "move_to",
                "target": {"x": step_index, "y": 64, "z": 0},
                "initial_distance": 4,
                "final_distance": 0 if step_index == 7 else 3,
                "progress_status": "reached" if step_index == 7 else "partial_progress",
            },
            observation={"position": {"x": step_index, "y": 64, "z": 0}},
            task_spec={"goal": "Reach a target."},
        )

    normal = memory.trajectory.context_payload(max_chars=12000, exclude_latest=False)
    compressed = memory.trajectory.context_payload(max_chars=350, exclude_latest=False)

    assert len(memory.trajectory.entries) == 7
    assert normal["recent_steps"][-1]["action_type"] == "move_to"
    assert normal["segments"][0]["phase"] == "navigation"
    assert compressed["compression"] in {"aggressive", "episode"}


def test_knowledge_previous_step_does_not_hide_last_world_action() -> None:
    """A knowledge turn is not in trajectory, so the prior world action must remain visible."""

    memory = RunContextMemory()
    memory.trajectory.record(
        step_index=0,
        action=HarnessAction(type="scan_blocks", args={"block": "oak_log"}),
        result={"ok": True, "blocks": []},
        observation={},
        task_spec={"goal": "Collect oak log."},
    )

    payload = memory.context_payload(
        max_chars=12000,
        max_knowledge_chars=3500,
        previous_step_index=1,
    )

    assert payload["trajectory"]["latest_step_is_in_compact_evidence"] is False
    assert payload["trajectory"]["recent_steps"][0]["step_index"] == 0


def test_skill_ledger_survives_checkpoint_and_projects_one_canonical_copy() -> None:
    """Injected skills remain structured and deduplicated after checkpoint recovery."""

    memory = RunContextMemory()
    summary = {
        "name": "collect_wood",
        "version": "0.1.0",
        "description": "Collect logs using reachable evidence-backed targets.",
        "strategy_summary": "Scan, approach, dig, and verify pickup.",
        "parameterized_plan": [{"step": "scan"}, {"step": "collect"}],
        "status": "promoted",
    }
    memory.skills.record(summary=summary, step_index=2)
    memory.skills.record(summary=summary, step_index=5)

    restored = RunContextMemory.from_json(memory.to_json())
    payload = restored.context_payload(
        max_chars=12000,
        max_knowledge_chars=3500,
        max_skill_chars=4000,
    )

    assert list(restored.skills.entries) == ["collect_wood@0.1.0"]
    assert restored.skills.entries["collect_wood@0.1.0"].injection_count == 2
    assert len(payload["skills"]["entries"]) == 1
    assert payload["skills"]["entries"][0]["identity"] == "collect_wood@0.1.0"
    assert payload["skills"]["entries"][0]["last_injected_step_index"] == 5


def test_agent_memory_resolves_entity_paths_without_an_action_field_map() -> None:
    """Entity selectors and JSON Pointers retain only values chosen by the model."""

    memory = RunContextMemory()
    memory.memory.record_source(
        step_index=2,
        action=HarnessAction(type="scan_entities", args={"entity": "sheep"}),
        result={
            "ok": True,
            "entities": [
                {
                    "entity_id": 68,
                    "name": "sheep",
                    "details": {
                        "metadata_decoded": {
                            "wool": {
                                "color": "brown",
                                "is_sheared": True,
                            }
                        }
                    },
                }
            ],
        },
    )

    outcomes = memory.memory.apply_updates(
        [
            MemoryUpdate(
                memory_key="entity:68/wool_state",
                source_ref="step:2/scan_entities/entity:68",
                paths=[
                    "/entity_id",
                    "/details/metadata_decoded/wool/color",
                    "/details/metadata_decoded/wool/is_sheared",
                ],
                note="This sheep is brown and has already been sheared.",
            )
        ],
        decision_step_index=3,
    )

    assert outcomes[0]["accepted"] is True
    entry = memory.memory.entries["entity:68/wool_state"]
    assert entry.selected_values == [
        {"path": "/entity_id", "value": 68},
        {
            "path": "/details/metadata_decoded/wool/color",
            "value": "brown",
        },
        {
            "path": "/details/metadata_decoded/wool/is_sheared",
            "value": True,
        },
    ]
    payload = memory.context_payload(
        max_chars=12000,
        max_knowledge_chars=1000,
        max_skill_chars=1000,
        max_memory_chars=3500,
    )
    assert payload["memory"]["entries"][0]["note"].startswith("This sheep is brown")
    assert "sources" not in payload["memory"]


def test_agent_memory_rejects_missing_paths_and_can_replace_an_evolving_fact() -> None:
    """Invalid pointers never become facts, while an explicit key supports replacement."""

    memory = RunContextMemory()
    memory.memory.record_source(
        step_index=1,
        action=HarnessAction(type="use_item", args={"entity_id": 68}),
        result={
            "ok": True,
            "entity_id": 68,
            "spawned_drops": [{"item": "brown_wool", "count": 1}],
        },
    )
    rejected = memory.memory.apply_updates(
        [
            MemoryUpdate(
                memory_key="entity:68/interaction",
                source_ref="step:1/action_result",
                paths=["/invented_field"],
                note="Must not be stored.",
            )
        ],
        decision_step_index=2,
    )
    accepted = memory.memory.apply_updates(
        [
            MemoryUpdate(
                memory_key="entity:68/interaction",
                source_ref="step:1/action_result",
                paths=["/entity_id", "/spawned_drops"],
                note="Shearing this entity produced brown_wool.",
            )
        ],
        decision_step_index=2,
    )

    assert rejected[0]["accepted"] is False
    assert rejected[0]["error_code"] == "path_not_found"
    assert accepted[0]["accepted"] is True
    assert list(memory.memory.entries) == ["entity:68/interaction"]

    restored = RunContextMemory.from_json(memory.to_json())
    assert restored.memory.entries["entity:68/interaction"].selected_values[1] == {
        "path": "/spawned_drops",
        "value": [{"item": "brown_wool", "count": 1}],
    }
    assert restored.memory.sources[1].action_type == "use_item"
