from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.db.models import (
    Base,
    RunRecord,
    SKILL_DELETED_STATUS,
    SkillRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.harness.context_manager import ContextManager
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus
from mc_agent_harness.skills.creation import derive_skill_name, select_relevant_skill_steps
from mc_agent_harness.skills.initial import seed_initial_skills
from mc_agent_harness.skills.library import SkillLibrary, SkillLibraryError, SkillSearchScope


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create an in-memory SQLite session factory with skill tables."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.parametrize(
    ("category", "task_id", "primary_target", "expected"),
    [
        ("combat", "combat_zombie_plains_barehand", "zombie", "defeat_zombie"),
        (
            "harvest",
            "harvest_wool_extreme_hills_with_shears",
            "white_wool",
            "harvest_white_wool",
        ),
        (
            "techtree",
            "techtree_iron_pickaxe",
            "iron_pickaxe",
            "techtree_iron_pickaxe",
        ),
        ("survival", "survival", "time_alive", "survival_time_alive"),
    ],
)
def test_programmatic_skill_name_follows_task_category(
    category: str,
    task_id: str,
    primary_target: str,
    expected: str,
) -> None:
    """The four programmatic task categories own stable Skill name prefixes."""

    run = RunRecord(
        id=f"run_{task_id}",
        task_id=task_id,
        status="succeeded",
        task_spec={"task_id": task_id, "category": category},
    )

    assert derive_skill_name(run, primary_target) == expected


def test_programmatic_skill_name_infers_category_for_legacy_run() -> None:
    """Older runs without task_spec.category still use their task-id category."""

    run = RunRecord(
        id="run_legacy_techtree",
        task_id="minedojo_techtree_diamond_pickaxe",
        status="succeeded",
        task_spec={"task_id": "minedojo_techtree_diamond_pickaxe"},
    )

    assert derive_skill_name(run, "diamond_pickaxe") == "techtree_diamond_pickaxe"


def _wood_actions() -> list[HarnessAction]:
    """Return the primitive starter-wood procedure used by skill tests."""

    return [
        HarnessAction(type="scan_blocks", args={"block": "oak_log", "count": 1}),
        HarnessAction(
            type="move_to", args={"position": {"x": 1, "y": 65, "z": 0}, "tolerance": 2.0}
        ),
        HarnessAction(
            type="dig_block_at", args={"block": "oak_log", "position": {"x": 1, "y": 65, "z": 0}}
        ),
        HarnessAction(type="wait_ticks", args={"ticks": 20}),
    ]


def _insert_successful_wood_run(session_factory: sessionmaker[Session]) -> None:
    """Seed a completed run that can be converted into a skill candidate."""

    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_collect_wood",
                task_id="minedojo_harvest_oak_log",
                status="completed",
                task_spec={
                    "task_id": "minedojo_harvest_oak_log",
                    "goal": "Harvest one oak log.",
                    "category": "harvest",
                    "family": "Harvest",
                    "allowed_actions": [
                        "query_inventory",
                        "scan_blocks",
                        "move_to",
                        "dig_block_at",
                        "wait_ticks",
                    ],
                    "knowledge_tags": ["minecraft:block/oak_log", "minecraft:harvest"],
                },
            )
        )
        for step_index, action in enumerate(_wood_actions()):
            session.add(
                StepRecord(
                    run_id="run_collect_wood",
                    step_index=step_index,
                    observation={
                        "inventory": [],
                        "nearby_blocks": [
                            {"name": "oak_log", "position": {"x": 1, "y": 65, "z": 0}}
                        ],
                    },
                    action=action.model_dump(mode="json"),
                    action_result={
                        "ok": True,
                        "action_type": action.type,
                        "inventory_delta": {"oak_log": 1} if action.type == "wait_ticks" else {},
                        "observation": {"inventory": [{"name": "oak_log", "count": 1}]}
                        if action.type == "wait_ticks"
                        else {"inventory": []},
                    },
                )
            )
        session.commit()


def _insert_query_only_run(session_factory: sessionmaker[Session]) -> None:
    """Seed a completed run that should not become a reusable skill."""

    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_query_only",
                task_id="minedojo_harvest_oak_log",
                status="completed",
                task_spec={
                    "task_id": "minedojo_harvest_oak_log",
                    "goal": "Harvest one oak log.",
                    "allowed_actions": [
                        "query_inventory",
                        "scan_blocks",
                        "move_to",
                        "dig_block_at",
                        "wait_ticks",
                    ],
                },
            )
        )
        session.add(
            StepRecord(
                run_id="run_query_only",
                step_index=0,
                observation={"inventory": [{"name": "oak_log", "count": 1}]},
                action={"type": "query_inventory", "args": {}},
                action_result={
                    "ok": True,
                    "action_type": "query_inventory",
                    "inventory": [{"name": "oak_log", "count": 1}],
                    "observation": {"inventory": [{"name": "oak_log", "count": 1}]},
                },
            )
        )
        session.commit()


def _insert_simple_craft_run(session_factory: sessionmaker[Session]) -> None:
    """Seed a one-step recipe trace that should remain knowledge-backed instead of promoted."""

    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_craft_planks",
                task_id="minedojo_techtree_oak_planks",
                status="completed",
                task_spec={
                    "task_id": "minedojo_techtree_oak_planks",
                    "goal": "Craft oak planks.",
                    "category": "techtree",
                    "family": "TechTree",
                    "knowledge_tags": ["minecraft:item/oak_planks"],
                    "verifier": {"type": "inventory_contains", "item": "oak_planks", "count": 4},
                },
            )
        )
        session.add(
            StepRecord(
                run_id="run_craft_planks",
                step_index=0,
                observation={"inventory": [{"name": "oak_log", "count": 1}]},
                action={"type": "craft_item", "args": {"item": "oak_planks", "count": 4}},
                action_result={
                    "ok": True,
                    "action_type": "craft_item",
                    "item": "oak_planks",
                    "expected_output_count": 4,
                    "observation": {"inventory": [{"name": "oak_planks", "count": 4}]},
                },
            )
        )
        session.commit()


def _insert_successful_combat_run(session_factory: sessionmaker[Session]) -> None:
    """Seed a bounded combat run that should become a contextual combat skill."""

    actions = [
        HarnessAction(type="scan_entities", args={"entity": "zombie", "max_distance": 16}),
        HarnessAction(
            type="engage_combat", args={"entity": "zombie", "mode": "melee", "retreat_health": 6}
        ),
    ]
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_defeat_zombie",
                task_id="minedojo_combat_zombie",
                status="completed",
                task_spec={
                    "task_id": "minedojo_combat_zombie",
                    "goal": "Defeat one zombie.",
                    "category": "combat",
                    "family": "Combat",
                    "verifier": {"type": "entity_kill_delta", "entity": "zombie", "count": 1},
                    "knowledge_tags": ["minecraft:entity/zombie", "minecraft:combat"],
                },
            )
        )
        for step_index, action in enumerate(actions):
            session.add(
                StepRecord(
                    run_id="run_defeat_zombie",
                    step_index=step_index,
                    observation={
                        "health": 20,
                        "food": 20,
                        "nearby_entities": [
                            {
                                "name": "zombie",
                                "position": {"x": 3, "y": 64, "z": 0},
                                "distance": 3,
                                "melee_reachable": True,
                            }
                        ],
                    },
                    action=action.model_dump(mode="json"),
                    action_result={
                        "ok": True,
                        "action_type": action.type,
                        "entity": "zombie",
                        "mode": "melee",
                        "status": "target_killed" if action.type == "engage_combat" else "scanned",
                        "kill_stat_delta": 1 if action.type == "engage_combat" else 0,
                        "observation": {"stats": {"kill_entity": {"zombie": 1}}},
                    },
                )
            )
        session.commit()


def _insert_combat_run_with_unrelated_self_defense(
    session_factory: sessionmaker[Session],
) -> None:
    """Seed a chicken task whose first successful combat action targets a stale slime."""

    actions = [
        HarnessAction(type="engage_combat", args={"entity": "slime", "mode": "melee"}),
        HarnessAction(type="move_to", args={"position": {"x": 4, "y": 64, "z": 4}, "tolerance": 2}),
        HarnessAction(type="engage_combat", args={"entity": "chicken", "mode": "melee"}),
    ]
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_defeat_chicken_with_stale_slime",
                task_id="combat_chicken_forest_wooden_sword",
                status="succeeded",
                task_spec={
                    "task_id": "combat_chicken_forest_wooden_sword",
                    "goal": "Defeat one chicken.",
                    "category": "combat",
                    "family": "Combat",
                    "verifier": {"type": "entity_kill_delta", "entity": "chicken", "count": 1},
                    "knowledge_tags": ["minecraft:entity/chicken", "minecraft:combat"],
                },
            )
        )
        for step_index, action in enumerate(actions):
            action_entity = action.args.get("entity")
            session.add(
                StepRecord(
                    run_id="run_defeat_chicken_with_stale_slime",
                    step_index=step_index,
                    observation={"inventory": [{"name": "wooden_sword", "count": 1}]},
                    action=action.model_dump(mode="json"),
                    action_result={
                        "ok": True,
                        "action_type": action.type,
                        "entity": action_entity,
                        "status": "target_killed" if action.type == "engage_combat" else None,
                        "observation": {
                            "stats": {
                                "confirmed_kill_entity": {
                                    str(action_entity): 1,
                                }
                                if action_entity
                                else {}
                            }
                        },
                    },
                )
            )
        session.commit()


def _insert_harvest_wool_run_with_incidental_combat(
    session_factory: sessionmaker[Session],
) -> None:
    """Seed a harvest run that fights a skeleton before shearing a sheep."""

    actions = [
        HarnessAction(type="scan_entities", args={"entity": "sheep", "max_distance": 64}),
        HarnessAction(
            type="move_to_and_engage_combat",
            args={"entity": "skeleton", "mode": "melee", "max_duration_ms": 10_000},
        ),
        HarnessAction(type="use_item", args={"item": "shears", "entity": "sheep"}),
    ]
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_harvest_white_wool_with_incidental_combat",
                task_id="harvest_wool_extreme_hills_with_shears",
                status="succeeded",
                task_spec={
                    "task_id": "harvest_wool_extreme_hills_with_shears",
                    "goal": "Shear a sheep in extreme hills with shears.",
                    "category": "harvest",
                    "family": "Harvest",
                    "verifier": {
                        "type": "inventory_contains",
                        "item": "white_wool",
                        "count": 1,
                        "require_delta": True,
                    },
                },
            )
        )
        for step_index, action in enumerate(actions):
            session.add(
                StepRecord(
                    run_id="run_harvest_white_wool_with_incidental_combat",
                    step_index=step_index,
                    observation={
                        "inventory": [{"name": "shears", "count": 1}],
                    },
                    action=action.model_dump(mode="json"),
                    action_result={
                        "ok": True,
                        "action_type": action.type,
                        "entity": action.args.get("entity"),
                        "status": (
                            "target_killed"
                            if action.type == "move_to_and_engage_combat"
                            else "completed"
                        ),
                        "inventory_delta": ({"white_wool": 1} if action.type == "use_item" else {}),
                        "observation": {
                            "inventory": [
                                {"name": "shears", "count": 1},
                                *(
                                    [{"name": "white_wool", "count": 1}]
                                    if action.type == "use_item"
                                    else []
                                ),
                            ]
                        },
                    },
                )
            )
        session.commit()


def _insert_successful_glass_run(session_factory: sessionmaker[Session]) -> None:
    """Seed a furnace workflow whose broad MineDojo tags should not control skill naming."""

    actions = [
        HarnessAction(type="scan_blocks", args={"block": "sand", "count": 10}),
        HarnessAction(
            type="dig_block_at", args={"block": "sand", "position": {"x": 4, "y": 64, "z": 4}}
        ),
        HarnessAction(type="place_block", args={"item": "furnace"}),
        HarnessAction(
            type="process_item",
            args={
                "station": "furnace",
                "output": "glass",
                "input": "sand",
                "fuel": "coal",
                "count": 1,
            },
        ),
    ]
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_smelt_glass",
                task_id="harvest_1_glass_swampland_with_furnace_and_fuel",
                status="completed",
                task_spec={
                    "task_id": "harvest_1_glass_swampland_with_furnace_and_fuel",
                    "goal": "find material and smelt to obtain glass in swampland given a furnace and fuels",
                    "category": "harvest",
                    "family": "Harvest",
                    "knowledge_tags": [
                        "minecraft:term/find",
                        "minecraft:term/fuel",
                        "minecraft:term/furnace",
                        "minecraft:term/glass",
                        "minecraft:term/smelt",
                    ],
                    "verifier": {
                        "type": "inventory_contains",
                        "item": "glass",
                        "count": 1,
                        "require_delta": True,
                    },
                },
            )
        )
        for step_index, action in enumerate(actions):
            session.add(
                StepRecord(
                    run_id="run_smelt_glass",
                    step_index=step_index,
                    observation={
                        "inventory": [
                            {"name": "coal", "count": 50},
                            {"name": "furnace", "count": 1},
                        ]
                    },
                    action=action.model_dump(mode="json"),
                    action_result={
                        "ok": True,
                        "action_type": action.type,
                        "item": action.args.get("item") or action.args.get("block"),
                        "inventory_delta": {"glass": 1} if action.type == "process_item" else {},
                        "observation": {"inventory": [{"name": "glass", "count": 1}]}
                        if action.type == "process_item"
                        else {
                            "inventory": [
                                {"name": "coal", "count": 50},
                                {"name": "furnace", "count": 1},
                            ]
                        },
                    },
                )
            )
        session.commit()


@pytest.mark.anyio
async def test_skill_library_creates_promotes_searches_and_exports_candidate(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _insert_successful_wood_run(session_factory)
    library = SkillLibrary(session_factory=session_factory, export_dir=tmp_path)

    candidate = await library.create_candidate("run_collect_wood")
    promoted = await library.promote(candidate)
    results = await library.search(
        "collect oak_log for harvest",
        scope=SkillSearchScope(
            task_id="minedojo_harvest_oak_log",
            task_tags=("minecraft:harvest",),
            canonical_ids=("oak_log",),
            allowed_actions=("dig_block_at",),
        ),
    )
    export_path = await library.export_markdown(promoted.name, promoted.version)

    assert candidate.name == "harvest_oak_log"
    assert candidate.source_step_range is not None
    assert candidate.source_step_range.start == 0
    assert candidate.validation["candidate_policy"] == "reusable_workflow"
    assert candidate.validation["parameterized_plan"][0]["type"] == "scan_blocks"
    assert (
        candidate.validation["parameterized_plan"][1]["target"] == "selected_block_or_drop_position"
    )
    assert candidate.strategy_summary is not None
    assert "do not replay source coordinates blindly" in candidate.strategy_summary
    assert candidate.parameterized_plan[0]["type"] == "scan_blocks"
    assert candidate.recovery_policy
    assert candidate.source_evidence["source_coordinates_are_replay_only"] is True
    assert promoted.status == SkillStatus.promoted
    assert results and results[0].name == "harvest_oak_log"
    assert export_path.exists()
    exported = export_path.read_text(encoding="utf-8")
    assert "scan_blocks" in exported
    assert "dig_block_at" in exported
    assert "wait_ticks" in exported

    with session_factory() as session:
        record = session.scalar(select(SkillRecord).where(SkillRecord.name == "harvest_oak_log"))
        assert record is not None
        assert record.status == SkillStatus.promoted.value
        event_types = [event.event_type for event in session.scalars(select(TrajectoryEventRecord))]
        assert "skill_candidate_created" in event_types
        assert "skill_promoted" in event_types
        assert "skill_exported" in event_types


@pytest.mark.anyio
async def test_skill_candidate_creation_is_idempotent_by_source_run(
    session_factory: sessionmaker[Session],
) -> None:
    """Checkpoint resume must not create a second skill version for one source run."""

    _insert_successful_wood_run(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    first = await library.create_candidate("run_collect_wood")
    second = await library.create_candidate("run_collect_wood")

    assert (first.name, first.version, first.source_run_id) == (
        second.name,
        second.version,
        second.source_run_id,
    )
    with session_factory() as session:
        records = session.scalars(
            select(SkillRecord).where(SkillRecord.source_run_id == "run_collect_wood")
        ).all()
        assert len(records) == 1


@pytest.mark.anyio
async def test_deleted_skill_is_not_read_or_reused_as_source_run_candidate(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """A tombstone remains version history but is invisible to reads and candidate reuse."""

    _insert_successful_wood_run(session_factory)
    library = SkillLibrary(session_factory=session_factory, export_dir=tmp_path)
    first = await library.create_candidate("run_collect_wood")
    with session_factory() as session:
        record = session.scalar(
            select(SkillRecord).where(
                SkillRecord.name == first.name,
                SkillRecord.version == first.version,
            )
        )
        assert record is not None
        deleted_id = record.id
        record.status = SKILL_DELETED_STATUS
        record.spec = {
            **record.spec,
            "_dashboard_deleted": {"authority": "dashboard_operator"},
        }
        session.commit()

    assert await library.get(first.name, first.version) is None
    assert await library.get(first.name) is None
    with pytest.raises(SkillLibraryError, match="Skill not found"):
        await library.export_markdown(deleted_id)

    recreated = await library.create_candidate("run_collect_wood")

    assert recreated.name == first.name
    assert recreated.version != first.version
    assert recreated.status == SkillStatus.draft
    with session_factory() as session:
        records = session.scalars(
            select(SkillRecord)
            .where(SkillRecord.source_run_id == "run_collect_wood")
            .order_by(SkillRecord.id)
        ).all()
        assert [record.status for record in records] == [
            SKILL_DELETED_STATUS,
            SkillStatus.draft.value,
        ]


@pytest.mark.anyio
async def test_skill_library_rejects_query_only_candidate(
    session_factory: sessionmaker[Session],
) -> None:
    _insert_query_only_run(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    with pytest.raises(SkillLibraryError, match="no successful progress actions"):
        await library.create_candidate("run_query_only")


@pytest.mark.anyio
async def test_skill_library_skips_simple_recipe_candidate(
    session_factory: sessionmaker[Session],
) -> None:
    _insert_simple_craft_run(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    with pytest.raises(SkillLibraryError, match="covered_by_recipe_knowledge"):
        await library.create_candidate("run_craft_planks")

    with session_factory() as session:
        assert session.scalars(select(SkillRecord)).all() == []
        event = session.scalar(
            select(TrajectoryEventRecord).where(
                TrajectoryEventRecord.event_type == "skill_candidate_policy_skipped"
            )
        )
        assert event is not None
        assert event.payload["reason"] == "covered_by_recipe_knowledge"


@pytest.mark.anyio
async def test_skill_library_creates_contextual_combat_candidate(
    session_factory: sessionmaker[Session],
) -> None:
    _insert_successful_combat_run(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    candidate = await library.create_candidate("run_defeat_zombie")

    assert candidate.name == "defeat_zombie"
    assert "bounded combat engagements" in candidate.strategy_summary
    assert "combat_mode_should_match_reachability" in candidate.preconditions
    assert candidate.parameterized_plan[0]["type"] == "scan_entities"
    assert candidate.parameterized_plan[1]["type"] == "engage_combat"
    assert candidate.parameterized_plan[1]["stop_policy"]
    assert any("low_health" in note for note in candidate.recovery_policy)
    assert "action:engage_combat" in candidate.dependencies


@pytest.mark.anyio
async def test_skill_library_excludes_unrelated_combat_from_entity_skill(
    session_factory: sessionmaker[Session],
) -> None:
    """A chicken skill should audit but not learn an unrelated stale-slime fight."""

    _insert_combat_run_with_unrelated_self_defense(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    candidate = await library.create_candidate("run_defeat_chicken_with_stale_slime")

    assert candidate.name == "defeat_chicken"
    assert [step["type"] for step in candidate.parameterized_plan] == ["move_to", "engage_combat"]
    assert candidate.parameterized_plan[-1]["target"] == "chicken"
    assert "slime" not in candidate.dependencies
    assert candidate.source_step_range is not None
    assert candidate.source_step_range.start == 1
    assert candidate.source_evidence["source_step_indexes"] == [1, 2]
    assert candidate.source_evidence["excluded_source_steps"] == [
        {
            "step_index": 0,
            "action_type": "engage_combat",
            "observed_target": "slime",
            "verifier_target": "chicken",
            "reason": "entity_target_mismatch",
        }
    ]
    assert candidate.validation["excluded_source_step_count"] == 1


@pytest.mark.anyio
async def test_harvest_skill_name_is_not_changed_by_incidental_combat(
    session_factory: sessionmaker[Session],
) -> None:
    """A combat action inside a harvest trace must not create defeat_<item>."""

    _insert_harvest_wool_run_with_incidental_combat(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    candidate = await library.create_candidate("run_harvest_white_wool_with_incidental_combat")

    assert candidate.name == "harvest_white_wool"
    assert any(step["type"] == "move_to_and_engage_combat" for step in candidate.parameterized_plan)
    assert candidate.source_evidence["primary_target"] == "white_wool"


def test_skill_evidence_keeps_entity_prerequisite_for_item_verifier() -> None:
    """Item tasks such as feather collection must retain prerequisite chicken combat."""

    run = RunRecord(
        id="run_collect_feather",
        task_id="harvest_1_feather",
        status="succeeded",
        task_spec={
            "task_id": "harvest_1_feather",
            "verifier": {"type": "inventory_contains", "item": "feather", "count": 1},
        },
    )
    step = StepRecord(
        run_id=run.id,
        step_index=0,
        observation={},
        action={"type": "engage_combat", "args": {"entity": "chicken", "mode": "melee"}},
        action_result={"ok": True, "action_type": "engage_combat", "entity": "chicken"},
    )

    selection = select_relevant_skill_steps(run, [step])

    assert selection.steps == [step]
    assert selection.excluded_steps == []
    assert selection.verifier_entity_target is None


@pytest.mark.anyio
async def test_skill_library_uses_harvest_category_for_furnace_skill_name(
    session_factory: sessionmaker[Session],
) -> None:
    """Furnace workflows should be named from the verifier target instead of broad prompt words."""

    _insert_successful_glass_run(session_factory)
    library = SkillLibrary(session_factory=session_factory)

    candidate = await library.create_candidate("run_smelt_glass")

    assert candidate.name == "harvest_glass"
    assert "glass" in candidate.triggers
    assert candidate.source_evidence["primary_target"] == "glass"
    assert candidate.parameterized_plan[-1]["type"] == "process_item"
    assert candidate.parameterized_plan[-1]["station"] == "furnace"


@pytest.mark.anyio
async def test_skill_library_deprecates_promoted_skill(
    session_factory: sessionmaker[Session],
) -> None:
    _insert_successful_wood_run(session_factory)
    library = SkillLibrary(session_factory=session_factory)
    candidate = await library.create_candidate("run_collect_wood")
    promoted = await library.promote(candidate)

    deprecated = await library.deprecate(promoted.name, promoted.version, reason="verifier drift")
    search_results = await library.search("collect oak_log", scope={"canonical_ids": ["oak_log"]})

    assert deprecated.status == SkillStatus.deprecated
    assert deprecated.metrics["deprecation_reason"] == "verifier drift"
    assert search_results == []


@pytest.mark.anyio
async def test_context_manager_injects_promoted_skill_summary(
    session_factory: sessionmaker[Session],
) -> None:
    library = SkillLibrary(session_factory=session_factory)
    skill = SkillSpec(
        name="collect_wood",
        version="0.1.0",
        description="Collect starter oak logs.",
        triggers=["oak_log", "wood", "harvest"],
        preconditions=["nearby_block:oak_log"],
        action_plan=_wood_actions(),
        task_scope=["minecraft:harvest", "action:dig_block_at"],
        dependencies=["oak_log", "action:dig_block_at"],
        status=SkillStatus.promoted,
    )
    with session_factory() as session:
        session.add(
            SkillRecord(
                name=skill.name,
                version=skill.version,
                status=skill.status.value,
                spec=skill.model_dump(mode="json"),
            )
        )
        session.commit()

    manager = ContextManager(skill_library=library)
    result = await manager.build(
        observation={"inventory": [], "nearby_blocks": [{"name": "oak_log"}]},
        task_memory=[],
        task_spec={
            "task_id": "minedojo_harvest_oak_log",
            "goal": "Harvest an oak log.",
            "knowledge_tags": ["minecraft:harvest"],
        },
        allowed_actions=["query_inventory", "scan_blocks", "move_to", "dig_block_at", "wait_ticks"],
    )

    assert result.retrieved_skills
    assert result.retrieved_skills[0].name == "collect_wood"
    payload = _message_payload(result.messages[1])
    assert payload["retrieved_skills"][0]["name"] == "collect_wood"
    assert payload["retrieved_skills"][0]["action_types"] == [
        "scan_blocks",
        "move_to",
        "dig_block_at",
        "wait_ticks",
    ]


@pytest.mark.anyio
async def test_context_manager_retrieves_initial_no_path_skill(
    session_factory: sessionmaker[Session],
) -> None:
    """A no_path previous step should surface bootstrap terrain-recovery skills."""

    result = seed_initial_skills(session_factory)
    assert result.created == 2
    assert seed_initial_skills(session_factory).unchanged == 2

    manager = ContextManager(skill_library=SkillLibrary(session_factory=session_factory))
    context = await manager.build(
        observation={
            "inventory": [{"name": "dirt", "count": 3}],
            "nearby_blocks": [{"name": "grass_block"}],
        },
        task_memory=[],
        task_spec={
            "task_id": "harvest_wool",
            "goal": "Harvest wool from a sheep.",
            "knowledge_tags": ["minecraft:harvest", "minecraft:entity/sheep"],
        },
        allowed_actions=[
            "query_inventory",
            "scan_blocks",
            "move_to",
            "dig_block_at",
            "place_block",
            "wait_ticks",
        ],
        previous_step={
            "action_type": "move_to",
            "error_code": "no_path",
            "progress_status": "no_path",
            "summary": "The target is not reachable; nearest_reachable_position is available.",
            "suggested_affordances": ["dig_block_at", "place_block", "scan_blocks"],
        },
    )

    names = [skill.name for skill in context.retrieved_skills]
    assert "recover_unreachable_by_digging" in names
    assert "gain_height_by_pillaring" in names


@pytest.mark.anyio
async def test_skill_library_finds_duplicate_candidates(
    session_factory: sessionmaker[Session],
) -> None:
    library = SkillLibrary(session_factory=session_factory)
    promoted = SkillSpec(
        name="collect_wood",
        version="0.1.0",
        description="Collect starter oak logs.",
        triggers=["oak_log", "wood", "harvest"],
        preconditions=["nearby_block:oak_log"],
        action_plan=_wood_actions(),
        task_scope=["minecraft:harvest", "action:dig_block_at"],
        dependencies=["oak_log", "action:dig_block_at"],
        status=SkillStatus.promoted,
    )
    candidate = SkillSpec(
        name="harvest_oak_log",
        version="0.1.0",
        description="Mine one oak log.",
        triggers=["oak_log", "collect", "harvest"],
        preconditions=["nearby_block:oak_log"],
        action_plan=_wood_actions(),
        task_scope=["minecraft:harvest", "action:dig_block_at"],
        dependencies=["oak_log", "action:dig_block_at"],
        status=SkillStatus.draft,
    )
    with session_factory() as session:
        session.add(
            SkillRecord(
                name=promoted.name,
                version=promoted.version,
                status=promoted.status.value,
                spec=promoted.model_dump(mode="json"),
            )
        )
        session.commit()

    matches = await library.find_duplicates(candidate, threshold=0.8)

    assert matches
    assert matches[0].skill.name == "collect_wood"
    assert matches[0].similarity >= 0.8


def _message_payload(message: dict[str, Any]) -> dict[str, Any]:
    """Parse a JSON user message payload from ContextManager output."""

    import json

    return json.loads(str(message["content"]))
