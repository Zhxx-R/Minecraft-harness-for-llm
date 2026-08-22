from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mc_agent_harness.api.routes.launcher import require_local_control
from mc_agent_harness.configuration import (
    PromptConfigEntry,
    PromptConfigSnapshot,
    PromptConfigurationConflictError,
    PromptConfigurationService,
    UnknownActionConfigurationError,
)
from mc_agent_harness.db.models import KnowledgeChunkRecord
from mc_agent_harness.db.session import SessionLocal
from mc_agent_harness.harness.tool_registry import (
    CANONICAL_CONTROL_ACTIONS,
    CANONICAL_KNOWLEDGE_ACTIONS,
    PROMPT_HIDDEN_ACTIONS,
)


router = APIRouter(tags=["configuration"])


class KnowledgeChunkView(BaseModel):
    """One versioned knowledge chunk exposed to the local dashboard."""

    id: str
    source: str
    title: str
    content: str
    tags: list[str]
    metadata: dict[str, Any]
    enabled: bool
    version: int
    has_embedding: bool
    created_at: datetime | None
    updated_at: datetime | None


class KnowledgeChunkPage(BaseModel):
    """One filtered page plus source and kind facets."""

    items: list[KnowledgeChunkView]
    total: int
    offset: int
    limit: int
    sources: dict[str, int]
    kinds: dict[str, int]


class KnowledgeChunkCreateRequest(BaseModel):
    """Editable fields used to create a managed knowledge chunk."""

    id: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=200_000)
    tags: list[str] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("id", "source", "title", "content")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """Reject whitespace-only identifiers and content."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        """Store a bounded, de-duplicated tag list."""

        return _normalize_string_list(value, max_items=64, max_chars=128)


class KnowledgeChunkUpdateRequest(BaseModel):
    """Complete editable knowledge state guarded by optimistic locking."""

    source: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=200_000)
    tags: list[str] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    expected_version: int = Field(ge=1)

    @field_validator("source", "title", "content")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """Reject whitespace-only editable text."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        """Store a bounded, de-duplicated tag list."""

        return _normalize_string_list(value, max_items=64, max_chars=128)


class VersionedMutationRequest(BaseModel):
    """Expected row version supplied by reset/archive controls."""

    expected_version: int = Field(ge=0)


class SystemPromptUpdateRequest(BaseModel):
    """Editable system prompt guarded by its resolved version."""

    content: str = Field(min_length=1, max_length=100_000)
    enabled: bool = True
    expected_version: int = Field(ge=0)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        """Reject an empty active prompt override."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("content must not be blank")
        return normalized


class HotReloadUpdateRequest(BaseModel):
    """Editable prompt reload policy guarded by its resolved version."""

    enabled: bool
    expected_version: int = Field(ge=0)


class ActionPromptUpdateRequest(BaseModel):
    """Editable prompt-facing metadata for one implemented action."""

    purpose: str = Field(max_length=10_000)
    args: dict[str, Any] = Field(default_factory=dict)
    returns: str = Field(max_length=10_000)
    when_to_use: str = Field(max_length=10_000)
    recommended_next_actions: list[str] = Field(default_factory=list, max_length=32)
    prompt_visible: bool = True
    expected_version: int = Field(ge=0)

    @field_validator("purpose", "returns", "when_to_use")
    @classmethod
    def strip_action_text(cls, value: str) -> str:
        """Normalize editable action prose."""

        return value.strip()

    @field_validator("recommended_next_actions")
    @classmethod
    def normalize_recommendations(cls, value: list[str]) -> list[str]:
        """Store bounded, de-duplicated recommendations."""

        return _normalize_string_list(value, max_items=32, max_chars=1000)


class SystemPromptConfigurationView(BaseModel):
    """Effective system prompt plus override provenance."""

    content: str
    enabled: bool
    version: int
    persisted: bool
    effective_source: str
    updated_at: datetime | None


class ActionPromptConfigurationView(BaseModel):
    """One effective action prompt description."""

    action_type: str
    display_name: str
    category: str
    runtime_supported: bool
    hard_hidden: bool
    prompt_visible: bool
    purpose: str
    args: dict[str, Any]
    returns: str
    when_to_use: str
    recommended_next_actions: list[str]
    version: int
    persisted: bool
    effective_source: str
    updated_at: datetime | None


class HotReloadConfigurationView(BaseModel):
    """Effective prompt reload policy plus override provenance."""

    enabled: bool
    version: int
    persisted: bool
    effective_source: str
    updated_at: datetime | None


class PromptConfigurationBundleView(BaseModel):
    """Atomic configuration bundle consumed by the frontend and agent."""

    hot_reload: HotReloadConfigurationView
    snapshot_revision: str
    system_prompt: SystemPromptConfigurationView
    actions: list[ActionPromptConfigurationView]


def get_configuration_session() -> Iterator[Session]:
    """Yield one SQL session for configuration routes."""

    with SessionLocal() as session:
        yield session


def get_prompt_configuration_service() -> PromptConfigurationService:
    """Return the SQL-backed prompt configuration service."""

    return PromptConfigurationService(SessionLocal)


@router.get("/knowledge-chunks", response_model=KnowledgeChunkPage)
def list_knowledge_chunks(
    q: str = Query(default="", max_length=200),
    source: str | None = Query(default=None, max_length=255),
    kind: str | None = Query(default=None, max_length=128),
    enabled: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(get_configuration_session),
) -> KnowledgeChunkPage:
    """Search managed knowledge with stable pagination and filter facets."""

    statement = select(KnowledgeChunkRecord).order_by(
        KnowledgeChunkRecord.source,
        KnowledgeChunkRecord.id,
    )
    normalized_query = q.strip().lower()
    if normalized_query:
        pattern = f"%{normalized_query}%"
        statement = statement.where(
            or_(
                func.lower(KnowledgeChunkRecord.id).like(pattern),
                func.lower(KnowledgeChunkRecord.source).like(pattern),
                func.lower(KnowledgeChunkRecord.title).like(pattern),
                func.lower(KnowledgeChunkRecord.content).like(pattern),
            )
        )
    if source:
        statement = statement.where(KnowledgeChunkRecord.source == source)
    if enabled is not None:
        statement = statement.where(KnowledgeChunkRecord.enabled.is_(enabled))

    filtered = list(session.scalars(statement))
    if kind:
        filtered = [record for record in filtered if _knowledge_kind(record) == kind]
    sources = Counter(record.source for record in filtered)
    kinds = Counter(_knowledge_kind(record) for record in filtered)
    page = filtered[offset : offset + limit]
    return KnowledgeChunkPage(
        items=[_knowledge_chunk_view(record) for record in page],
        total=len(filtered),
        offset=offset,
        limit=limit,
        sources=dict(sorted(sources.items())),
        kinds=dict(sorted(kinds.items())),
    )


@router.post(
    "/knowledge-chunks",
    response_model=KnowledgeChunkView,
    status_code=201,
    dependencies=[Depends(require_local_control)],
)
def create_knowledge_chunk(
    request: KnowledgeChunkCreateRequest,
    session: Session = Depends(get_configuration_session),
) -> KnowledgeChunkView:
    """Create one immediately retrievable knowledge chunk."""

    if session.get(KnowledgeChunkRecord, request.id) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "knowledge_chunk_exists", "id": request.id},
        )
    record = KnowledgeChunkRecord(
        id=request.id,
        source=request.source,
        title=request.title,
        content=request.content,
        tags=request.tags,
        chunk_metadata=request.metadata,
        enabled=request.enabled,
        version=1,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "knowledge_chunk_exists", "id": request.id},
        ) from exc
    session.refresh(record)
    return _knowledge_chunk_view(record)


@router.patch(
    "/knowledge-chunks/{chunk_id}",
    response_model=KnowledgeChunkView,
    dependencies=[Depends(require_local_control)],
)
def update_knowledge_chunk(
    chunk_id: str,
    request: KnowledgeChunkUpdateRequest,
    session: Session = Depends(get_configuration_session),
) -> KnowledgeChunkView:
    """Replace editable chunk fields under an optimistic version lock."""

    record = _locked_knowledge_chunk(session, chunk_id)
    _check_knowledge_version(record, request.expected_version)
    record.source = request.source
    record.title = request.title
    record.content = request.content
    record.tags = request.tags
    record.chunk_metadata = request.metadata
    record.enabled = request.enabled
    record.version += 1
    session.commit()
    session.refresh(record)
    return _knowledge_chunk_view(record)


@router.post(
    "/knowledge-chunks/{chunk_id}/archive",
    response_model=KnowledgeChunkView,
    dependencies=[Depends(require_local_control)],
)
def archive_knowledge_chunk(
    chunk_id: str,
    request: VersionedMutationRequest,
    session: Session = Depends(get_configuration_session),
) -> KnowledgeChunkView:
    """Soft-disable one chunk while preserving its audit history."""

    record = _locked_knowledge_chunk(session, chunk_id)
    _check_knowledge_version(record, request.expected_version)
    record.enabled = False
    record.version += 1
    session.commit()
    session.refresh(record)
    return _knowledge_chunk_view(record)


@router.get("/prompt-configurations", response_model=PromptConfigurationBundleView)
def get_prompt_configurations(
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> PromptConfigurationBundleView:
    """Return the atomic effective prompt snapshot used for hot reload."""

    return _prompt_bundle_view(service.snapshot())


@router.put(
    "/prompt-configurations/hot-reload",
    response_model=HotReloadConfigurationView,
    dependencies=[Depends(require_local_control)],
)
def put_hot_reload_configuration(
    request: HotReloadUpdateRequest,
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> HotReloadConfigurationView:
    """Save the versioned prompt hot-reload policy."""

    try:
        entry = service.put_hot_reload(
            enabled=request.enabled,
            expected_version=request.expected_version,
        )
    except PromptConfigurationConflictError as exc:
        raise _prompt_conflict(exc) from exc
    return _hot_reload_view(entry)


@router.delete(
    "/prompt-configurations/hot-reload",
    response_model=HotReloadConfigurationView,
    dependencies=[Depends(require_local_control)],
)
def reset_hot_reload_configuration(
    request: VersionedMutationRequest,
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> HotReloadConfigurationView:
    """Reset prompt hot reload to its enabled code default."""

    try:
        entry = service.reset_hot_reload(expected_version=request.expected_version)
    except PromptConfigurationConflictError as exc:
        raise _prompt_conflict(exc) from exc
    return _hot_reload_view(entry)


@router.put(
    "/prompt-configurations/system",
    response_model=SystemPromptConfigurationView,
    dependencies=[Depends(require_local_control)],
)
def put_system_prompt_configuration(
    request: SystemPromptUpdateRequest,
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> SystemPromptConfigurationView:
    """Save a versioned system-prompt override."""

    try:
        entry = service.put_system(
            content=request.content,
            enabled=request.enabled,
            expected_version=request.expected_version,
        )
    except PromptConfigurationConflictError as exc:
        raise _prompt_conflict(exc) from exc
    return _system_prompt_view(entry)


@router.delete(
    "/prompt-configurations/system",
    response_model=SystemPromptConfigurationView,
    dependencies=[Depends(require_local_control)],
)
def reset_system_prompt_configuration(
    request: VersionedMutationRequest,
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> SystemPromptConfigurationView:
    """Reset the system prompt to the code default."""

    try:
        entry = service.reset_system(expected_version=request.expected_version)
    except PromptConfigurationConflictError as exc:
        raise _prompt_conflict(exc) from exc
    return _system_prompt_view(entry)


@router.put(
    "/prompt-configurations/actions/{action_type}",
    response_model=ActionPromptConfigurationView,
    dependencies=[Depends(require_local_control)],
)
def put_action_prompt_configuration(
    action_type: str,
    request: ActionPromptUpdateRequest,
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> ActionPromptConfigurationView:
    """Save a prompt override for one implemented action."""

    try:
        entry = service.put_action(
            action_type=action_type,
            purpose=request.purpose,
            args=request.args,
            returns=request.returns,
            when_to_use=request.when_to_use,
            recommended_next_actions=request.recommended_next_actions,
            prompt_visible=request.prompt_visible,
            expected_version=request.expected_version,
        )
    except UnknownActionConfigurationError as exc:
        raise _unknown_action(exc) from exc
    except PromptConfigurationConflictError as exc:
        raise _prompt_conflict(exc) from exc
    return _action_prompt_view(entry)


@router.delete(
    "/prompt-configurations/actions/{action_type}",
    response_model=ActionPromptConfigurationView,
    dependencies=[Depends(require_local_control)],
)
def reset_action_prompt_configuration(
    action_type: str,
    request: VersionedMutationRequest,
    service: PromptConfigurationService = Depends(get_prompt_configuration_service),
) -> ActionPromptConfigurationView:
    """Reset one implemented action guide to its code default."""

    try:
        entry = service.reset_action(
            action_type=action_type,
            expected_version=request.expected_version,
        )
    except UnknownActionConfigurationError as exc:
        raise _unknown_action(exc) from exc
    except PromptConfigurationConflictError as exc:
        raise _prompt_conflict(exc) from exc
    return _action_prompt_view(entry)


def _knowledge_chunk_view(record: KnowledgeChunkRecord) -> KnowledgeChunkView:
    """Convert a knowledge ORM record into its public representation."""

    return KnowledgeChunkView(
        id=record.id,
        source=record.source,
        title=record.title,
        content=record.content,
        tags=list(record.tags or []),
        metadata=dict(record.chunk_metadata or {}),
        enabled=record.enabled,
        version=record.version,
        has_embedding=record.embedding is not None,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _knowledge_kind(record: KnowledgeChunkRecord) -> str:
    """Return a stable kind facet from arbitrary chunk metadata."""

    value = (record.chunk_metadata or {}).get("kind")
    normalized = str(value or "unspecified").strip()
    return normalized or "unspecified"


def _locked_knowledge_chunk(session: Session, chunk_id: str) -> KnowledgeChunkRecord:
    """Load one knowledge row for an atomic versioned mutation."""

    record = session.scalar(
        select(KnowledgeChunkRecord).where(KnowledgeChunkRecord.id == chunk_id).with_for_update()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="knowledge_chunk_not_found")
    return record


def _check_knowledge_version(
    record: KnowledgeChunkRecord,
    expected_version: int,
) -> None:
    """Reject stale chunk mutations with a refreshable conflict payload."""

    if record.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_knowledge_chunk_version",
                "expected_version": expected_version,
                "current_version": record.version,
            },
        )


def _prompt_bundle_view(snapshot: PromptConfigSnapshot) -> PromptConfigurationBundleView:
    """Convert one resolved snapshot into the frontend contract."""

    system = _system_prompt_view(snapshot.system)
    actions = [
        _action_prompt_view(snapshot.actions[action_type])
        for action_type in sorted(snapshot.actions)
    ]
    return PromptConfigurationBundleView(
        hot_reload=_hot_reload_view(snapshot.hot_reload),
        snapshot_revision=snapshot.revision,
        system_prompt=system,
        actions=actions,
    )


def _hot_reload_view(entry: PromptConfigEntry) -> HotReloadConfigurationView:
    """Project the prompt reload policy without storage-only fields."""

    enabled = entry.payload.get("enabled")
    return HotReloadConfigurationView(
        enabled=enabled if isinstance(enabled, bool) else True,
        version=entry.version,
        persisted=entry.persisted,
        effective_source="database_override" if entry.persisted else "code_default",
        updated_at=entry.updated_at,
    )


def _system_prompt_view(entry: PromptConfigEntry) -> SystemPromptConfigurationView:
    """Project one system entry without exposing storage-only fields."""

    return SystemPromptConfigurationView(
        content=str(entry.payload.get("content") or ""),
        enabled=entry.enabled,
        version=entry.version,
        persisted=entry.persisted,
        effective_source=(
            "database_override" if entry.persisted and entry.enabled else "code_default"
        ),
        updated_at=entry.updated_at,
    )


def _action_prompt_view(entry: PromptConfigEntry) -> ActionPromptConfigurationView:
    """Project one action entry with implementation and visibility metadata."""

    payload = entry.payload
    action_type = entry.config_key
    hard_hidden = action_type in PROMPT_HIDDEN_ACTIONS
    return ActionPromptConfigurationView(
        action_type=action_type,
        display_name=entry.display_name,
        category=_action_category(action_type),
        runtime_supported=True,
        hard_hidden=hard_hidden,
        prompt_visible=bool(payload.get("prompt_visible", True)) and not hard_hidden,
        purpose=str(payload.get("purpose") or ""),
        args=dict(payload.get("args") or {}),
        returns=str(payload.get("returns") or ""),
        when_to_use=str(payload.get("when_to_use") or ""),
        recommended_next_actions=[
            str(item) for item in payload.get("recommended_next_actions", []) if str(item).strip()
        ],
        version=entry.version,
        persisted=entry.persisted,
        effective_source="database_override" if entry.persisted else "code_default",
        updated_at=entry.updated_at,
    )


def _action_category(action_type: str) -> str:
    """Group action contracts into stable configuration-center sections."""

    if action_type in CANONICAL_KNOWLEDGE_ACTIONS:
        return "knowledge"
    if action_type in CANONICAL_CONTROL_ACTIONS:
        return "control"
    if action_type in PROMPT_HIDDEN_ACTIONS:
        return "compatibility"
    return "runtime"


def _prompt_conflict(exc: PromptConfigurationConflictError) -> HTTPException:
    """Translate service concurrency errors into a stable HTTP conflict."""

    return HTTPException(
        status_code=409,
        detail={
            "code": "stale_prompt_configuration_version",
            "kind": exc.kind,
            "config_key": exc.config_key,
            "expected_version": exc.expected_version,
            "current_version": exc.current_version,
        },
    )


def _unknown_action(exc: UnknownActionConfigurationError) -> HTTPException:
    """Translate unknown runtime actions into an explicit validation response."""

    return HTTPException(
        status_code=422,
        detail={
            "code": "action_not_implemented",
            "action_type": exc.action_type,
        },
    )


def _normalize_string_list(
    values: list[str],
    *,
    max_items: int,
    max_chars: int,
) -> list[str]:
    """Normalize user-edited string lists while preserving stable order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()[:max_chars]
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
        if len(normalized) >= max_items:
            break
    return normalized
