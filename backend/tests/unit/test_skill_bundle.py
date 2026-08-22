from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from mc_agent_harness.db.models import SKILL_DELETED_STATUS, Base, RunRecord, SkillRecord
from mc_agent_harness.db.session import create_database_engine, create_session_factory
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus
from mc_agent_harness.skills.bundle import export_skill_bundle, import_skill_bundle


def _database(path: Path):
    engine = create_database_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_skill_bundle_round_trip_detaches_missing_source_run(tmp_path: Path) -> None:
    """Portable skills retain JSON provenance without requiring the remote run table."""

    source_factory = _database(tmp_path / "source.sqlite3")
    source_run_id = "remote-run-1"
    spec = SkillSpec(
        name="collect_remote_wood",
        version="0.1.0",
        description="Collect wood using a verified remote trajectory.",
        source_run_id=source_run_id,
        status=SkillStatus.promoted,
    )
    with source_factory() as session:
        session.add(
            RunRecord(
                id=source_run_id,
                task_id="harvest_1_log",
                status="succeeded",
                task_spec={"task_id": "harvest_1_log"},
            )
        )
        session.add(
            SkillRecord(
                name=spec.name,
                version=spec.version,
                status=spec.status.value,
                spec=spec.model_dump(mode="json"),
                source_run_id=source_run_id,
            )
        )
        session.commit()

    bundle_path = tmp_path / "skills.json"
    exported = export_skill_bundle(
        source_factory,
        bundle_path,
        learned_only=True,
    )
    target_factory = _database(tmp_path / "target.sqlite3")
    imported = import_skill_bundle(target_factory, bundle_path)

    assert exported.skill_count == 1
    assert imported.created == 1
    assert imported.detached_source_runs == 1
    with target_factory() as session:
        record = session.scalar(select(SkillRecord))
        assert record is not None
        assert record.source_run_id is None
        assert SkillSpec.model_validate(record.spec).source_run_id == source_run_id

    repeated = import_skill_bundle(target_factory, bundle_path)
    assert repeated.skipped == 1

    reexported = export_skill_bundle(
        target_factory,
        tmp_path / "reexported.json",
        learned_only=True,
    )
    assert reexported.skill_count == 1


def test_skill_bundle_rejects_modified_content(tmp_path: Path) -> None:
    """The importer should fail closed when portable skill content is edited in transit."""

    source_factory = _database(tmp_path / "source.sqlite3")
    spec = SkillSpec(
        name="verified_skill",
        version="0.1.0",
        description="Original description.",
        status=SkillStatus.promoted,
    )
    with source_factory() as session:
        session.add(
            SkillRecord(
                name=spec.name,
                version=spec.version,
                status=spec.status.value,
                spec=spec.model_dump(mode="json"),
            )
        )
        session.commit()

    bundle_path = tmp_path / "skills.json"
    export_skill_bundle(source_factory, bundle_path)
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["skills"][0]["spec"]["description"] = "Tampered description."
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        import_skill_bundle(_database(tmp_path / "target.sqlite3"), bundle_path)


def test_skill_bundle_unfiltered_export_excludes_deleted_tombstones(tmp_path: Path) -> None:
    """An empty status filter never turns permanent deletion metadata into a portable skill."""

    session_factory = _database(tmp_path / "source.sqlite3")
    active = SkillSpec(
        name="active_skill",
        version="0.1.0",
        description="Visible portable skill.",
        status=SkillStatus.promoted,
    )
    deleted = SkillSpec(
        name="deleted_skill",
        version="0.1.0",
        description="Historical tombstone payload.",
        status=SkillStatus.promoted,
    )
    with session_factory() as session:
        session.add_all(
            [
                SkillRecord(
                    name=active.name,
                    version=active.version,
                    status=active.status.value,
                    spec=active.model_dump(mode="json"),
                ),
                SkillRecord(
                    name=deleted.name,
                    version=deleted.version,
                    status=SKILL_DELETED_STATUS,
                    spec={
                        **deleted.model_dump(mode="json"),
                        "_dashboard_deleted": {"authority": "dashboard_operator"},
                    },
                ),
            ]
        )
        session.commit()

    bundle_path = tmp_path / "all-statuses.json"
    exported = export_skill_bundle(session_factory, bundle_path, statuses=())
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))

    assert exported.skill_count == 1
    assert payload["filters"]["statuses"] == []
    assert [item["name"] for item in payload["skills"]] == ["active_skill"]
