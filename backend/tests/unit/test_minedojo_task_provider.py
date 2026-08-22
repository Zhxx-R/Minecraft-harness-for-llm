from pathlib import Path

import pytest

from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider, TaskManifestNotFound


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_DIR = ROOT / "tasks" / "manifests"


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_minedojo_task_provider_lists_curated_week6_tasks() -> None:
    provider = MineDojoTaskProvider(MANIFEST_DIR)

    tasks = await provider.list_tasks()

    assert len(tasks) == 10
    assert {task["category"] for task in tasks} == {"harvest", "techtree", "combat"}
    assert "minedojo_harvest_oak_log" in {task["task_id"] for task in tasks}


@pytest.mark.anyio
async def test_minedojo_task_provider_loads_task_and_verifies_run_state() -> None:
    provider = MineDojoTaskProvider(MANIFEST_DIR)
    task = await provider.load_task("minedojo_harvest_oak_log")

    result = await provider.verify(
        {
            "task_spec": task,
            "steps": [
                {
                    "action_result": {
                        "ok": True,
                        "observation": {"inventory": [{"name": "oak_log", "count": 1}]},
                    }
                }
            ],
        }
    )

    assert result["success"] is True


@pytest.mark.anyio
async def test_minedojo_task_provider_raises_for_missing_task() -> None:
    provider = MineDojoTaskProvider(MANIFEST_DIR)

    with pytest.raises(TaskManifestNotFound):
        await provider.load_task("missing_task")


@pytest.mark.anyio
async def test_minedojo_task_provider_indexes_manifests_once() -> None:
    """Repeated task loads should reuse one process-local JSON manifest cache."""

    provider = CountingTaskProvider(MANIFEST_DIR)

    await provider.list_tasks()
    first_read_count = provider.file_reads
    await provider.load_task("minedojo_harvest_oak_log")
    await provider.load_task("minedojo_harvest_dirt")

    assert first_read_count > 0
    assert provider.file_reads == first_read_count


class CountingTaskProvider(MineDojoTaskProvider):
    """Task provider test double that counts source-file parses."""

    def __init__(self, manifest_dir: Path) -> None:
        super().__init__(manifest_dir)
        self.file_reads = 0

    def _load_manifest_file(self, path: Path) -> list[dict[str, object]]:
        """Count one source parse before delegating to the provider implementation."""

        self.file_reads += 1
        return super()._load_manifest_file(path)
