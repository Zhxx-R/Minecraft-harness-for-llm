from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Sequence

from sqlalchemy import select

from mc_agent_harness.db.models import SKILL_DELETED_STATUS, RunRecord, SkillRecord
from mc_agent_harness.db.session import SessionFactory
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus


SKILL_BUNDLE_SCHEMA_VERSION = "mc-agent-harness.skill-bundle.v1"
ConflictPolicy = Literal["skip", "replace", "error"]


@dataclass(frozen=True, slots=True)
class SkillBundleExportResult:
    """Summary of one portable skill-bundle export."""

    output_path: str
    checksum_path: str
    skill_count: int
    statuses: tuple[str, ...]
    learned_only: bool
    sha256: str


@dataclass(frozen=True, slots=True)
class SkillBundleImportResult:
    """Summary of one validated skill-bundle import."""

    bundle_skill_count: int
    created: int
    replaced: int
    skipped: int
    detached_source_runs: int
    on_conflict: str


def export_skill_bundle(
    session_factory: SessionFactory,
    output_path: str | Path,
    *,
    statuses: Sequence[SkillStatus | str] = (SkillStatus.promoted,),
    learned_only: bool = False,
) -> SkillBundleExportResult:
    """Export validated canonical skill specs without database credentials or run payloads."""

    status_values = _normalize_statuses(statuses)
    statement = (
        select(SkillRecord)
        .where(SkillRecord.status != SKILL_DELETED_STATUS)
        .order_by(SkillRecord.name, SkillRecord.version)
    )
    if status_values:
        statement = statement.where(SkillRecord.status.in_(status_values))

    with session_factory() as session:
        records = session.scalars(statement).all()
        skills = [_portable_record(record) for record in records]
    if learned_only:
        skills = [record for record in skills if record["source_run_id"] is not None]

    payload = {
        "schema_version": SKILL_BUNDLE_SCHEMA_VERSION,
        "exported_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "filters": {
            "statuses": list(status_values),
            "learned_only": learned_only,
        },
        "skill_count": len(skills),
        "skills_sha256": _canonical_sha256(skills),
        "skills": skills,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
    file_sha256 = _file_sha256(path)
    checksum_path = path.with_name(f"{path.name}.sha256")
    checksum_path.write_text(f"{file_sha256}  {path.name}\n", encoding="utf-8")
    return SkillBundleExportResult(
        output_path=str(path),
        checksum_path=str(checksum_path),
        skill_count=len(skills),
        statuses=status_values,
        learned_only=learned_only,
        sha256=file_sha256,
    )


def import_skill_bundle(
    session_factory: SessionFactory,
    bundle_path: str | Path,
    *,
    on_conflict: ConflictPolicy = "skip",
) -> SkillBundleImportResult:
    """Validate and import portable skills while safely detaching unavailable source runs."""

    if on_conflict not in {"skip", "replace", "error"}:
        raise ValueError(f"Unsupported conflict policy: {on_conflict}")
    path = Path(bundle_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Skill bundle must contain a JSON object.")
    if payload.get("schema_version") != SKILL_BUNDLE_SCHEMA_VERSION:
        raise ValueError("Unsupported skill bundle schema_version.")
    raw_skills = payload.get("skills")
    if not isinstance(raw_skills, list):
        raise ValueError("Skill bundle must contain a skills list.")
    if payload.get("skill_count") != len(raw_skills):
        raise ValueError("Skill bundle skill_count does not match the skills list.")
    if payload.get("skills_sha256") != _canonical_sha256(raw_skills):
        raise ValueError("Skill bundle content checksum mismatch.")

    validated = [_validated_portable_record(item) for item in raw_skills]
    identities = [(spec.name, spec.version) for spec, _source_run_id in validated]
    if len(identities) != len(set(identities)):
        raise ValueError("Skill bundle contains duplicate name/version identities.")

    created = 0
    replaced = 0
    skipped = 0
    detached_source_runs = 0
    with session_factory() as session:
        for spec, source_run_id in validated:
            existing = session.scalar(
                select(SkillRecord).where(
                    SkillRecord.name == spec.name,
                    SkillRecord.version == spec.version,
                )
            )
            if existing is not None and on_conflict == "skip":
                skipped += 1
                continue
            if existing is not None and on_conflict == "error":
                raise ValueError(f"Skill already exists: {spec.name}:{spec.version}")

            attached_source_run_id = source_run_id
            if source_run_id is not None and session.get(RunRecord, source_run_id) is None:
                attached_source_run_id = None
                detached_source_runs += 1
            spec_payload = spec.model_dump(mode="json")
            if existing is None:
                session.add(
                    SkillRecord(
                        name=spec.name,
                        version=spec.version,
                        status=spec.status.value,
                        spec=spec_payload,
                        source_run_id=attached_source_run_id,
                    )
                )
                created += 1
            else:
                existing.status = spec.status.value
                existing.spec = spec_payload
                existing.source_run_id = attached_source_run_id
                replaced += 1
        session.commit()

    return SkillBundleImportResult(
        bundle_skill_count=len(validated),
        created=created,
        replaced=replaced,
        skipped=skipped,
        detached_source_runs=detached_source_runs,
        on_conflict=on_conflict,
    )


def result_json(result: SkillBundleExportResult | SkillBundleImportResult) -> dict[str, object]:
    """Convert a bundle operation result to a stable JSON object."""

    return asdict(result)


def _portable_record(record: SkillRecord) -> dict[str, object]:
    """Validate one SQL row and serialize only portable skill metadata."""

    spec = SkillSpec.model_validate(record.spec)
    if record.status != spec.status.value:
        raise ValueError(
            f"Skill status mismatch for {record.name}:{record.version}: "
            f"row={record.status}, spec={spec.status.value}"
        )
    return {
        "name": spec.name,
        "version": spec.version,
        "status": spec.status.value,
        # SkillSpec keeps portable provenance even when the target database cannot
        # attach its optional SQL foreign key to the remote run table.
        "source_run_id": spec.source_run_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "spec": spec.model_dump(mode="json"),
    }


def _validated_portable_record(payload: object) -> tuple[SkillSpec, str | None]:
    """Validate one untrusted portable skill record."""

    if not isinstance(payload, dict):
        raise ValueError("Every skill bundle entry must be a JSON object.")
    spec = SkillSpec.model_validate(payload.get("spec"))
    if payload.get("name") != spec.name or payload.get("version") != spec.version:
        raise ValueError(f"Skill identity mismatch for {spec.name}:{spec.version}.")
    if payload.get("status") != spec.status.value:
        raise ValueError(f"Skill status mismatch for {spec.name}:{spec.version}.")
    source_run_id = payload.get("source_run_id")
    if source_run_id is not None and not isinstance(source_run_id, str):
        raise ValueError(f"Invalid source_run_id for {spec.name}:{spec.version}.")
    if source_run_id != spec.source_run_id:
        raise ValueError(f"Skill source_run_id mismatch for {spec.name}:{spec.version}.")
    return spec, source_run_id


def _normalize_statuses(statuses: Sequence[SkillStatus | str]) -> tuple[str, ...]:
    """Validate and deterministically order requested skill statuses."""

    return tuple(sorted({SkillStatus(status).value for status in statuses}))


def _canonical_sha256(payload: object) -> str:
    """Hash JSON content independently of presentation whitespace."""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """Hash one bundle file without loading it a second time into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
