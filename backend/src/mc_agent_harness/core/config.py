from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Environment-driven application settings."""

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://mc_agent:mc_agent@localhost:5432/mc_agent"
    redis_url: str = "redis://localhost:6379/0"
    model_default: str = "qwen3.7-plus"
    qwen_base_url: str | None = None
    qwen_api_key: str | None = None
    mineflayer_worker_url: str = "ws://localhost:8765"
    mineclip_scorer_url: str = "http://127.0.0.1:8091"
    minecraft_host: str = "127.0.0.1"
    minecraft_port: int = 25565
    minecraft_rcon_host: str = "127.0.0.1"
    minecraft_rcon_port: int = 25575
    minecraft_rcon_password: str | None = None
    mc_agent_spectator_player: str | None = None
    mc_agent_recording_window_title: str = "Minecraft"
    mc_agent_spectator_wait_sec: float = 300.0
    mc_agent_spectator_chunk_sync_delay_sec: float = 0.75
    mc_agent_spectator_rebind_interval_sec: float = 10.0
    mc_agent_spectator_full_sync_interval_sec: float = 0.0
    mc_agent_spectator_resync_distance_blocks: float = 96.0
    mc_agent_spectator_resync_cooldown_sec: float = 30.0
    mc_agent_stop_server_after_run: bool = True
    artifact_root: str = "runs"

    @field_validator("artifact_root")
    @classmethod
    def normalize_artifact_root(cls, value: str) -> str:
        """Resolve relative artifact paths against the repository instead of process cwd."""

        path = Path(value).expanduser()
        return str((path if path.is_absolute() else PROJECT_ROOT / path).resolve())

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")


settings = Settings()
