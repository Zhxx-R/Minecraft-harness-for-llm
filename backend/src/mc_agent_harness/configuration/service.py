from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mc_agent_harness.configuration.defaults import (
    ACTION_PROMPT_KIND,
    DEFAULT_SYSTEM_PROMPT,
    HARD_HIDDEN_ACTIONS,
    HOT_RELOAD_KEY,
    IMPLEMENTED_ACTIONS,
    RUNTIME_SETTING_KIND,
    SYSTEM_PROMPT_KEY,
    SYSTEM_PROMPT_KIND,
    default_action_payload,
    default_hot_reload_payload,
    default_system_payload,
)
from mc_agent_harness.db.models import PromptConfigurationRecord
from mc_agent_harness.db.session import SessionFactory


class PromptConfigurationConflictError(ValueError):
    """Raised when a stale editor attempts to mutate a prompt override."""

    def __init__(
        self,
        *,
        kind: str,
        config_key: str,
        expected_version: int,
        current_version: int,
    ) -> None:
        self.kind = kind
        self.config_key = config_key
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Stale {kind} configuration {config_key}: expected "
            f"{expected_version}, current {current_version}."
        )


class UnknownActionConfigurationError(ValueError):
    """Raised when configuration targets an action with no runtime implementation."""

    def __init__(self, action_type: str) -> None:
        self.action_type = action_type
        super().__init__(f"Action is not implemented by the harness: {action_type}")


@dataclass(frozen=True, slots=True)
class PromptConfigEntry:
    """Resolved default or persisted prompt configuration."""

    kind: str
    config_key: str
    display_name: str
    enabled: bool
    payload: dict[str, Any]
    version: int
    persisted: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-ready configuration view."""

        return {
            "kind": self.kind,
            "config_key": self.config_key,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "payload": self.payload,
            "version": self.version,
            "persisted": self.persisted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class PromptConfigSnapshot:
    """One internally consistent prompt configuration snapshot."""

    system: PromptConfigEntry
    actions: dict[str, PromptConfigEntry]
    hot_reload: PromptConfigEntry = field(default_factory=lambda: _default_hot_reload_entry())

    @property
    def revision(self) -> str:
        """Return the shared API/runtime revision for this atomic snapshot."""

        encoded = json.dumps(
            self.configuration_versions(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    @property
    def system_prompt(self) -> str:
        """Return the enabled override, or the safe code default."""

        if not self.system.enabled:
            return DEFAULT_SYSTEM_PROMPT
        content = self.system.payload.get("content")
        return (
            str(content) if isinstance(content, str) and content.strip() else DEFAULT_SYSTEM_PROMPT
        )

    @property
    def hot_reload_enabled(self) -> bool:
        """Return whether running agents should read prompts on every decision."""

        enabled = self.hot_reload.payload.get("enabled")
        return enabled if isinstance(enabled, bool) else True

    def action_guides(self, allowed_actions: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """Resolve enabled prompt-facing guides for one task action scope."""

        guides: list[dict[str, Any]] = []
        for action_type in allowed_actions:
            entry = self.actions.get(action_type)
            if entry is None or not entry.enabled or action_type in HARD_HIDDEN_ACTIONS:
                continue
            payload = entry.payload
            if not bool(payload.get("prompt_visible", True)):
                continue
            guides.append(
                {
                    "type": action_type,
                    "purpose": payload.get("purpose", ""),
                    "args": payload.get("args", {}),
                    "returns": payload.get("returns", ""),
                    "when_to_use": payload.get("when_to_use", ""),
                    "recommended_next_actions": payload.get(
                        "recommended_next_actions",
                        [],
                    ),
                }
            )
        return guides

    def recommended_next_actions(self, action_type: str) -> list[str]:
        """Return configured follow-up guidance for one executed action."""

        entry = self.actions.get(action_type)
        if entry is None or not entry.enabled:
            return []
        value = entry.payload.get("recommended_next_actions")
        return [str(item) for item in value] if isinstance(value, list) else []

    def configuration_versions(self) -> dict[str, Any]:
        """Return a compact version-only audit projection."""

        return {
            "hot_reload": {
                "version": self.hot_reload.version,
                "persisted": self.hot_reload.persisted,
                "enabled": self.hot_reload_enabled,
            },
            "system": {
                "version": self.system.version,
                "persisted": self.system.persisted,
                "enabled": self.system.enabled,
            },
            "actions": {
                action_type: {
                    "version": entry.version,
                    "persisted": entry.persisted,
                    "enabled": entry.enabled,
                    "prompt_visible": bool(entry.payload.get("prompt_visible", True)),
                }
                for action_type, entry in sorted(self.actions.items())
            },
        }

    def to_json(self) -> dict[str, Any]:
        """Return the complete bundle consumed by the configuration API."""

        return {
            "hot_reload": self.hot_reload.to_json(),
            "system": self.system.to_json(),
            "actions": [
                self.actions[action_type].to_json() for action_type in sorted(self.actions)
            ],
        }


class DatabasePromptConfigProvider:
    """Read effective prompt configuration from SQL on every snapshot."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def snapshot(self) -> PromptConfigSnapshot:
        """Overlay persisted rows on deterministic code defaults."""

        system = _default_system_entry()
        hot_reload = _default_hot_reload_entry()
        actions = {
            action_type: _default_action_entry(action_type) for action_type in IMPLEMENTED_ACTIONS
        }
        with self.session_factory() as session:
            records = list(
                session.scalars(
                    select(PromptConfigurationRecord).order_by(
                        PromptConfigurationRecord.kind,
                        PromptConfigurationRecord.config_key,
                    )
                )
            )
        for record in records:
            entry = _entry_from_record(record)
            if record.kind == SYSTEM_PROMPT_KIND and record.config_key == SYSTEM_PROMPT_KEY:
                system = entry
            elif record.kind == RUNTIME_SETTING_KIND and record.config_key == HOT_RELOAD_KEY:
                hot_reload = entry
            elif record.kind == ACTION_PROMPT_KIND and record.config_key in actions:
                actions[record.config_key] = entry
        return PromptConfigSnapshot(
            system=system,
            actions=actions,
            hot_reload=hot_reload,
        )


class PromptConfigurationService:
    """Persist prompt overrides with optimistic version checks."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory
        self.provider = DatabasePromptConfigProvider(session_factory)

    def snapshot(self) -> PromptConfigSnapshot:
        """Return the current effective configuration bundle."""

        return self.provider.snapshot()

    def put_system(
        self,
        *,
        content: str,
        enabled: bool,
        expected_version: int,
    ) -> PromptConfigEntry:
        """Create or update the system-prompt override."""

        return self._put(
            kind=SYSTEM_PROMPT_KIND,
            config_key=SYSTEM_PROMPT_KEY,
            display_name="Agent system prompt",
            enabled=enabled,
            payload={"content": content},
            expected_version=expected_version,
        )

    def reset_system(self, *, expected_version: int) -> PromptConfigEntry:
        """Delete the system override and return the code default."""

        self._reset(
            kind=SYSTEM_PROMPT_KIND,
            config_key=SYSTEM_PROMPT_KEY,
            expected_version=expected_version,
        )
        return _default_system_entry()

    def put_hot_reload(
        self,
        *,
        enabled: bool,
        expected_version: int,
    ) -> PromptConfigEntry:
        """Create or update the prompt hot-reload policy."""

        return self._put(
            kind=RUNTIME_SETTING_KIND,
            config_key=HOT_RELOAD_KEY,
            display_name="Prompt hot reload",
            enabled=True,
            payload={"enabled": enabled},
            expected_version=expected_version,
        )

    def reset_hot_reload(self, *, expected_version: int) -> PromptConfigEntry:
        """Delete the policy override and return the enabled code default."""

        self._reset(
            kind=RUNTIME_SETTING_KIND,
            config_key=HOT_RELOAD_KEY,
            expected_version=expected_version,
        )
        return _default_hot_reload_entry()

    def put_action(
        self,
        *,
        action_type: str,
        purpose: str,
        args: dict[str, Any],
        returns: str,
        when_to_use: str,
        recommended_next_actions: list[str],
        prompt_visible: bool,
        expected_version: int,
    ) -> PromptConfigEntry:
        """Create or update one implemented action's prompt override."""

        _validate_action_type(action_type)
        return self._put(
            kind=ACTION_PROMPT_KIND,
            config_key=action_type,
            display_name=action_type,
            enabled=True,
            payload={
                "purpose": purpose,
                "args": args,
                "returns": returns,
                "when_to_use": when_to_use,
                "recommended_next_actions": recommended_next_actions,
                "prompt_visible": prompt_visible,
            },
            expected_version=expected_version,
        )

    def reset_action(
        self,
        *,
        action_type: str,
        expected_version: int,
    ) -> PromptConfigEntry:
        """Delete one action override and return its code default."""

        _validate_action_type(action_type)
        self._reset(
            kind=ACTION_PROMPT_KIND,
            config_key=action_type,
            expected_version=expected_version,
        )
        return _default_action_entry(action_type)

    def _put(
        self,
        *,
        kind: str,
        config_key: str,
        display_name: str,
        enabled: bool,
        payload: dict[str, Any],
        expected_version: int,
    ) -> PromptConfigEntry:
        """Upsert one row only when the caller holds the current version."""

        with self.session_factory() as session:
            record = session.scalar(
                select(PromptConfigurationRecord)
                .where(
                    PromptConfigurationRecord.kind == kind,
                    PromptConfigurationRecord.config_key == config_key,
                )
                .with_for_update()
            )
            current_version = record.version if record is not None else 0
            if current_version != expected_version:
                raise PromptConfigurationConflictError(
                    kind=kind,
                    config_key=config_key,
                    expected_version=expected_version,
                    current_version=current_version,
                )
            if record is None:
                record = PromptConfigurationRecord(
                    kind=kind,
                    config_key=config_key,
                    display_name=display_name,
                    enabled=enabled,
                    payload=payload,
                    version=1,
                )
                session.add(record)
            else:
                record.display_name = display_name
                record.enabled = enabled
                record.payload = payload
                record.version += 1
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                current_version = session.scalar(
                    select(PromptConfigurationRecord.version).where(
                        PromptConfigurationRecord.kind == kind,
                        PromptConfigurationRecord.config_key == config_key,
                    )
                )
                raise PromptConfigurationConflictError(
                    kind=kind,
                    config_key=config_key,
                    expected_version=expected_version,
                    current_version=int(current_version or 0),
                ) from exc
            session.refresh(record)
            return _entry_from_record(record)

    def _reset(
        self,
        *,
        kind: str,
        config_key: str,
        expected_version: int,
    ) -> None:
        """Remove one override only when the caller holds its current version."""

        with self.session_factory() as session:
            record = session.scalar(
                select(PromptConfigurationRecord)
                .where(
                    PromptConfigurationRecord.kind == kind,
                    PromptConfigurationRecord.config_key == config_key,
                )
                .with_for_update()
            )
            current_version = record.version if record is not None else 0
            if current_version != expected_version:
                raise PromptConfigurationConflictError(
                    kind=kind,
                    config_key=config_key,
                    expected_version=expected_version,
                    current_version=current_version,
                )
            if record is not None:
                session.delete(record)
                session.commit()


def _default_system_entry() -> PromptConfigEntry:
    """Build the non-persisted system default exposed as version zero."""

    return PromptConfigEntry(
        kind=SYSTEM_PROMPT_KIND,
        config_key=SYSTEM_PROMPT_KEY,
        display_name="Agent system prompt",
        enabled=True,
        payload=default_system_payload(),
        version=0,
        persisted=False,
    )


def _default_hot_reload_entry() -> PromptConfigEntry:
    """Build the non-persisted hot-reload default exposed as version zero."""

    return PromptConfigEntry(
        kind=RUNTIME_SETTING_KIND,
        config_key=HOT_RELOAD_KEY,
        display_name="Prompt hot reload",
        enabled=True,
        payload=default_hot_reload_payload(),
        version=0,
        persisted=False,
    )


def _default_action_entry(action_type: str) -> PromptConfigEntry:
    """Build one non-persisted action default exposed as version zero."""

    return PromptConfigEntry(
        kind=ACTION_PROMPT_KIND,
        config_key=action_type,
        display_name=action_type,
        enabled=True,
        payload=default_action_payload(action_type),
        version=0,
        persisted=False,
    )


def _entry_from_record(record: PromptConfigurationRecord) -> PromptConfigEntry:
    """Convert one ORM row into an immutable runtime/API entry."""

    return PromptConfigEntry(
        kind=record.kind,
        config_key=record.config_key,
        display_name=record.display_name,
        enabled=record.enabled,
        payload=dict(record.payload or {}),
        version=record.version,
        persisted=True,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _validate_action_type(action_type: str) -> None:
    """Reject prompt entries that have no executable harness action."""

    if action_type not in IMPLEMENTED_ACTIONS:
        raise UnknownActionConfigurationError(action_type)
