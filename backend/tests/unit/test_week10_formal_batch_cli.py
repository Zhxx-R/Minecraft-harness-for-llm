from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "run_week10_formal_batch.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "run_week10_formal_batch_script",
    SCRIPT_PATH,
)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
FORMAL_SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = FORMAL_SCRIPT
SCRIPT_SPEC.loader.exec_module(FORMAL_SCRIPT)


def test_formal_batch_command_uses_two_servers_and_five_retries(tmp_path: Path) -> None:
    """The formal wrapper should encode the conservative local Week 10 defaults."""

    args = SimpleNamespace(
        manifest=ROOT / "tasks" / "executable" / "minedojo_programmatic_tasks.jsonl",
        task_count=100,
        worker_concurrency=2,
        include_survival=False,
        server_pool_state=tmp_path / "server_pool_state.json",
        max_task_retries=5,
        max_steps_per_task=30,
        max_runtime_sec_per_task=600.0,
        database_url="postgresql+psycopg://mc_agent:secret@localhost:5432/mc_agent",
        no_threat_pause=False,
        no_auto_promote=False,
    )

    command = FORMAL_SCRIPT._live_training_command(args, tmp_path / "report.json")
    rendered = FORMAL_SCRIPT._shell_command(command)

    assert command[command.index("--worker-concurrency") + 1] == "2"
    assert command[command.index("--max-task-retries") + 1] == "5"
    assert command[command.index("--diverse-batch-size") + 1] == "100"
    assert "--rcon-random-teleport-when-biome-missing" in command
    assert "--checkpoint-path" in command
    assert "--threat-pause" in command
    assert "--auto-promote" in command
    assert "secret" not in rendered
    assert "mc_agent:***@localhost" in rendered


def test_formal_batch_can_include_complete_1581_task_catalog(tmp_path: Path) -> None:
    """The server-scale formal wrapper should opt into the two survival tasks."""

    args = SimpleNamespace(
        manifest=ROOT / "tasks" / "executable" / "minedojo_programmatic_tasks.jsonl",
        task_count=1581,
        worker_concurrency=5,
        max_task_similarity=1.0,
        include_survival=True,
        server_pool_state=tmp_path / "server_pool_state.json",
        max_task_retries=5,
        max_steps_per_task=30,
        max_runtime_sec_per_task=600.0,
        database_url="postgresql+psycopg://mc_agent:secret@localhost:5432/mc_agent",
        no_threat_pause=False,
        no_auto_promote=False,
    )

    command = FORMAL_SCRIPT._live_training_command(args, tmp_path / "report.json")

    assert command[command.index("--diverse-batch-size") + 1] == "1581"
    assert command[command.index("--worker-concurrency") + 1] == "5"
    assert command[command.index("--max-task-similarity") + 1] == "1.0"
    category_values = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--category"
    ]
    assert category_values == ["harvest", "combat", "techtree", "survival"]
