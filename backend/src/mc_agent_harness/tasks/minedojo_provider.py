from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mc_agent_harness.evaluation.verifiers import ProgrammaticVerifier


class TaskManifestNotFound(KeyError):
    """Raised when a requested MineDojo-derived task manifest is unavailable."""


class MineDojoTaskProvider:
    """Loads MineDojo-derived tasks from local harness manifests or JSONL snapshots."""

    def __init__(
        self,
        manifest_dir: str | Path = "tasks/manifests",
        verifier: ProgrammaticVerifier | None = None,
    ) -> None:
        self.manifest_dir = Path(manifest_dir)
        self.verifier = verifier or ProgrammaticVerifier()
        self._manifest_cache: tuple[dict[str, Any], ...] | None = None
        self._manifest_by_id: dict[str, dict[str, Any]] | None = None

    async def list_tasks(self) -> list[dict[str, Any]]:
        """Return imported MineDojo task summaries sorted by task id."""

        tasks = [self._summary(manifest) for manifest in self._load_all_manifests()]
        return sorted(tasks, key=lambda task: str(task["task_id"]))

    async def load_task(self, task_id: str) -> dict[str, Any]:
        """Return a MineDojo-derived harness task specification."""

        self._ensure_manifest_cache()
        assert self._manifest_by_id is not None
        manifest = self._manifest_by_id.get(task_id)
        if manifest is not None:
            return manifest
        raise TaskManifestNotFound(f"Task manifest not found: {task_id}")

    async def verify(self, run_state: dict[str, Any]) -> dict[str, Any]:
        """Verify a run using MineDojo-derived success metadata."""

        task_spec = run_state.get("task_spec")
        if not isinstance(task_spec, dict):
            task_id = run_state.get("task_id")
            if not isinstance(task_id, str):
                return {"success": False, "reason": "run_state must include task_spec or task_id.", "checks": []}
            task_spec = await self.load_task(task_id)
        return await self.verifier.verify(task_spec, run_state)

    def _load_all_manifests(self) -> list[dict[str, Any]]:
        """Load every JSON task manifest under the configured manifest path."""

        self._ensure_manifest_cache()
        assert self._manifest_cache is not None
        return list(self._manifest_cache)

    def _ensure_manifest_cache(self) -> None:
        """Build one immutable process-local manifest cache and task-id index."""

        if self._manifest_cache is not None:
            return
        if not self.manifest_dir.exists():
            manifests: list[dict[str, Any]] = []
        elif self.manifest_dir.is_file():
            manifests = self._load_manifest_file(self.manifest_dir)
        else:
            manifests = []
            paths = sorted(
                [
                    *self.manifest_dir.rglob("*.json"),
                    *self.manifest_dir.rglob("*.jsonl"),
                ]
            )
            for path in paths:
                manifests.extend(self._load_manifest_file(path))
        by_id: dict[str, dict[str, Any]] = {}
        for manifest in manifests:
            task_id = manifest.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            if task_id in by_id:
                raise ValueError(f"Duplicate task_id in manifest source: {task_id}")
            by_id[task_id] = manifest
        self._manifest_cache = tuple(manifests)
        self._manifest_by_id = by_id

    def _load_manifest_file(self, path: Path) -> list[dict[str, Any]]:
        """Load a single manifest file, supporting JSON object, JSON array, and JSONL."""

        if path.suffix == ".jsonl":
            manifests = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    manifests.append(payload)
            return manifests
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return [payload] if isinstance(payload, dict) else []

    def _summary(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Build a compact task summary for task listing and UI previews."""

        benchmark = manifest.get("benchmark") if isinstance(manifest.get("benchmark"), dict) else {}
        minedojo = manifest.get("minedojo") if isinstance(manifest.get("minedojo"), dict) else {}
        verifier = manifest.get("verifier") if isinstance(manifest.get("verifier"), dict) else {}
        calibration = verifier.get("calibration") if isinstance(verifier.get("calibration"), dict) else {}
        return {
            "task_id": manifest.get("task_id"),
            "source": manifest.get("source"),
            "category": manifest.get("category"),
            "family": manifest.get("family"),
            "goal": manifest.get("goal"),
            "allowed_actions": manifest.get("allowed_actions", []),
            "knowledge_tags": manifest.get("knowledge_tags", []),
            "seed": benchmark.get("seed"),
            "max_steps": benchmark.get("max_steps"),
            "creative": bool(minedojo.get("creative")),
            "collection": minedojo.get("collection"),
            "calibration_status": calibration.get("status"),
        }
