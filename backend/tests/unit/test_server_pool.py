from mc_agent_harness.runtime.server_pool import (
    build_local_server_pool,
    estimate_server_pool_resources,
    load_server_pool_state,
)


def test_server_pool_estimator_recommends_two_servers_on_32gb_machine() -> None:
    """Local Week10 training should default to conservative two-server isolation."""

    pool = build_local_server_pool(root_dir="/tmp/mc-pool", server_count=2, heap_gb=3.0)
    estimate = estimate_server_pool_resources(pool, total_memory_gb=32.0)

    assert [server.server_port for server in pool] == [25565, 25566]
    assert [server.rcon_port for server in pool] == [25575, 25576]
    assert estimate.recommended_max_servers == 2
    assert estimate.recommendation == "within_conservative_local_training_budget"


def test_server_pool_estimator_flags_large_local_pool() -> None:
    """Four local Minecraft servers should be reported above the conservative default."""

    pool = build_local_server_pool(root_dir="/tmp/mc-pool", server_count=4, heap_gb=3.0)
    estimate = estimate_server_pool_resources(pool, total_memory_gb=32.0)

    assert estimate.server_count == 4
    assert estimate.recommendation == "above_conservative_default_for_local_training"


def test_server_pool_state_round_trip_preserves_isolated_endpoints(tmp_path) -> None:
    """Persisted pool state should load two distinct game, RCON, and world placements."""

    import json

    pool = build_local_server_pool(
        root_dir=tmp_path / "pool",
        server_count=2,
        first_server_port=25565,
        first_rcon_port=25575,
        heap_gb=2.5,
    )
    state_path = tmp_path / "server_pool_state.json"
    state_path.write_text(
        json.dumps({"started_at": "2026-07-12T00:00:00Z", "servers": [item.to_json() for item in pool]}),
        encoding="utf-8",
    )

    loaded, state = load_server_pool_state(state_path)

    assert [server.server_port for server in loaded] == [25565, 25566]
    assert [server.rcon_port for server in loaded] == [25575, 25576]
    assert len({server.world_dir for server in loaded}) == 2
    assert state["started_at"] == "2026-07-12T00:00:00Z"
