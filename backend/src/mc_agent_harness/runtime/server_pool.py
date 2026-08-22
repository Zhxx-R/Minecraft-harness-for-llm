from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MinecraftServerInstanceSpec:
    """One isolated Minecraft server instance used for multi-port live training."""

    server_id: str
    host: str
    server_port: int
    rcon_port: int | None
    world_dir: str
    heap_gb: float = 3.0
    max_workers: int = 1

    def to_json(self) -> dict[str, Any]:
        """Convert the server instance spec into a JSON-safe payload."""

        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> MinecraftServerInstanceSpec:
        """Validate and load one server placement from a persisted pool state."""

        try:
            server_id = str(payload["server_id"])
            host = str(payload["host"])
            server_port = int(payload["server_port"])
            world_dir = str(payload["world_dir"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Minecraft server pool entry: {payload!r}") from exc
        rcon_value = payload.get("rcon_port")
        rcon_port = int(rcon_value) if rcon_value is not None else None
        spec = cls(
            server_id=server_id,
            host=host,
            server_port=server_port,
            rcon_port=rcon_port,
            world_dir=world_dir,
            heap_gb=float(payload.get("heap_gb", 3.0)),
            max_workers=int(payload.get("max_workers", 1)),
        )
        if not spec.server_id or not spec.host:
            raise ValueError("Minecraft server pool entries require server_id and host.")
        if not 1 <= spec.server_port <= 65535:
            raise ValueError(f"Invalid Minecraft server port: {spec.server_port}.")
        if spec.rcon_port is not None and not 1 <= spec.rcon_port <= 65535:
            raise ValueError(f"Invalid Minecraft RCON port: {spec.rcon_port}.")
        if spec.max_workers <= 0:
            raise ValueError("Minecraft server max_workers must be positive.")
        return spec


@dataclass(frozen=True, slots=True)
class ServerPoolResourceEstimate:
    """Conservative local resource estimate for an isolated server pool."""

    server_count: int
    worker_count: int
    estimated_java_heap_gb: float
    estimated_worker_ram_gb: float
    estimated_total_ram_gb: float
    recommended_max_servers: int
    recommendation: str

    def to_json(self) -> dict[str, Any]:
        """Convert the estimate into a JSON-safe payload."""

        return asdict(self)


def build_local_server_pool(
    *,
    root_dir: str | Path,
    server_count: int = 2,
    first_server_port: int = 25565,
    first_rcon_port: int = 25575,
    heap_gb: float = 3.0,
    host: str = "127.0.0.1",
) -> list[MinecraftServerInstanceSpec]:
    """Build deterministic local server specs for multi-port isolated training."""

    if server_count <= 0:
        raise ValueError("server_count must be positive.")
    root = Path(root_dir)
    return [
        MinecraftServerInstanceSpec(
            server_id=f"server-{index + 1}",
            host=host,
            server_port=first_server_port + index,
            rcon_port=first_rcon_port + index,
            world_dir=str(root / f"server-{index + 1}" / "world"),
            heap_gb=heap_gb,
            max_workers=1,
        )
        for index in range(server_count)
    ]


def load_server_pool_state(path: str | Path) -> tuple[list[MinecraftServerInstanceSpec], dict[str, Any]]:
    """Load isolated server placements and raw audit metadata from a pool state file."""

    state_path = Path(path)
    if not state_path.is_file():
        raise FileNotFoundError(f"Minecraft server pool state was not found: {state_path}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Minecraft server pool state must contain a JSON object.")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        raise ValueError("Minecraft server pool state must contain a non-empty servers list.")
    servers = [
        MinecraftServerInstanceSpec.from_json(item)
        for item in raw_servers
        if isinstance(item, dict)
    ]
    if len(servers) != len(raw_servers):
        raise ValueError("Every Minecraft server pool entry must be a JSON object.")
    server_ids = [server.server_id for server in servers]
    if len(server_ids) != len(set(server_ids)):
        raise ValueError("Minecraft server pool server_id values must be unique.")
    endpoints = [(server.host, server.server_port) for server in servers]
    if len(endpoints) != len(set(endpoints)):
        raise ValueError("Minecraft server pool game endpoints must be unique.")
    rcon_endpoints = [
        (server.host, server.rcon_port)
        for server in servers
        if server.rcon_port is not None
    ]
    if len(rcon_endpoints) != len(set(rcon_endpoints)):
        raise ValueError("Minecraft server pool RCON endpoints must be unique.")
    return servers, payload


def estimate_server_pool_resources(
    server_pool: list[MinecraftServerInstanceSpec],
    *,
    worker_ram_gb: float = 0.35,
    fixed_overhead_gb: float = 4.0,
    total_memory_gb: float | None = None,
) -> ServerPoolResourceEstimate:
    """Estimate whether a local server pool fits the current machine."""

    memory_gb = total_memory_gb or _local_memory_gb()
    worker_count = sum(max(1, spec.max_workers) for spec in server_pool)
    java_heap = sum(spec.heap_gb for spec in server_pool)
    worker_ram = worker_count * worker_ram_gb
    total = java_heap + worker_ram + fixed_overhead_gb
    recommended_max = 2 if memory_gb <= 36 else 3
    if total > memory_gb * 0.75:
        recommendation = "too_high_reduce_server_count_or_heap"
    elif len(server_pool) > recommended_max:
        recommendation = "above_conservative_default_for_local_training"
    else:
        recommendation = "within_conservative_local_training_budget"
    return ServerPoolResourceEstimate(
        server_count=len(server_pool),
        worker_count=worker_count,
        estimated_java_heap_gb=round(java_heap, 2),
        estimated_worker_ram_gb=round(worker_ram, 2),
        estimated_total_ram_gb=round(total, 2),
        recommended_max_servers=recommended_max,
        recommendation=recommendation,
    )


def _local_memory_gb() -> float:
    """Return approximate local physical memory in GiB when available."""

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        physical_pages = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return 32.0
    return (page_size * physical_pages) / (1024**3)
