from pathlib import Path

from mc_agent_harness.core.config import ENV_FILE, PROJECT_ROOT, Settings


def test_settings_load_repository_env_independent_of_working_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert ENV_FILE == PROJECT_ROOT / ".env"
    assert Path(settings.model_config["env_file"]) == ENV_FILE
