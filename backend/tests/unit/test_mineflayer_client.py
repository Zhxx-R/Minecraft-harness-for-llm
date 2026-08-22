import json
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import mc_agent_harness.runtime.mineflayer_client as mineflayer_client_module
from mc_agent_harness.runtime.mineflayer_client import MineflayerClient, MineflayerRpcError
from mc_agent_harness.schemas.action import HarnessAction
from websockets.asyncio.server import serve


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_mineflayer_client_rpc_round_trip() -> None:
    async with fake_worker() as worker_url:
        client = MineflayerClient(worker_url, request_timeout=1)

        await client.reset({"runtime": {"host": "localhost", "port": 25565}})
        observation = await client.observe()
        action_result = await client.act(HarnessAction(type="query_inventory", args={}))
        snapshot = await client.snapshot()
        await client.close()

        assert observation["health"] == 20
        assert action_result == {"ok": True, "inventory": []}
        assert snapshot["image"] is None
        assert [event.event for event in client.lifecycle_events] == ["connected", "closed"]


@pytest.mark.anyio
async def test_mineflayer_client_raises_rpc_errors() -> None:
    async with fake_worker() as worker_url:
        client = MineflayerClient(worker_url, request_timeout=1)

        with pytest.raises(MineflayerRpcError, match="Unsupported method"):
            await client._request("unsupported", {})

        await client.close()


@pytest.mark.anyio
async def test_mineflayer_client_act_timeout_probes_responsive_worker() -> None:
    """An unknown action result should retain evidence that the RPC process still responds."""

    async with fake_slow_act_worker() as worker_url:
        client = MineflayerClient(worker_url, request_timeout=0.05)

        result = await client.act(
            HarnessAction(
                type="dig_block_at",
                args={"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}, "timeout_ms": 25},
            )
        )
        await client.close()

        assert result["ok"] is False
        assert result["action_type"] == "dig_block_at"
        assert result["error_code"] == "rpc_timeout"
        assert result["terminated"] is False
        assert result["recoverable"] is True
        assert result["requires_worker_restart"] is True
        assert result["worker_health"]["responsive"] is True
        assert result["worker_health"]["observation"] == {"ok": True}
        assert client.lifecycle_events[-1].event == "action_rpc_timeout"


@pytest.mark.anyio
async def test_mineflayer_client_act_timeout_marks_unresponsive_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed health probe should make worker loss explicit in the action result."""

    monkeypatch.setattr(mineflayer_client_module, "ACTION_TIMEOUT_HEALTH_PROBE_SEC", 0.05)
    async with fake_serial_slow_worker() as worker_url:
        client = MineflayerClient(worker_url, request_timeout=0.05)

        result = await client.act(
            HarnessAction(
                type="dig_block_at",
                args={"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}, "timeout_ms": 25},
            )
        )
        await client.close()

        assert result["error_code"] == "rpc_timeout"
        assert result["terminated"] is True
        assert result["recoverable"] is False
        assert result["worker_health"]["responsive"] is False


@asynccontextmanager
async def fake_worker() -> AsyncIterator[str]:
    """Run a small JSON-RPC worker server for backend contract tests."""

    async def handler(websocket) -> None:
        await websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "worker.event",
                    "params": {
                        "event": "connected",
                        "timestamp": "2026-06-22T00:00:00Z",
                        "payload": {"worker": "fake"},
                    },
                }
            )
        )

        async for raw_message in websocket:
            request = json.loads(raw_message)
            method = request["method"]
            request_id = request["id"]

            if method == "reset":
                result = {"ok": True}
            elif method == "observe":
                result = {
                    "position": {"x": 0, "y": 64, "z": 0},
                    "health": 20,
                    "food": 20,
                    "inventory": [],
                    "nearby_entities": [],
                    "nearby_blocks": [],
                }
            elif method == "act":
                result = {"ok": True, "inventory": []}
            elif method == "snapshot":
                result = {"image": None, "format": None, "reason": "fake"}
            elif method == "close":
                await websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "worker.event",
                            "params": {
                                "event": "closed",
                                "timestamp": "2026-06-22T00:00:01Z",
                                "payload": {"worker": "fake"},
                            },
                        }
                    )
                )
                result = {"ok": True}
            else:
                await websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32601, "message": "Unsupported method"},
                        }
                    )
                )
                continue

            await websocket.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}))

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def fake_slow_act_worker() -> AsyncIterator[str]:
    """Run a JSON-RPC worker whose action response exceeds the client watchdog."""

    async def handler(websocket) -> None:
        async for raw_message in websocket:
            request = json.loads(raw_message)
            request_id = request["id"]
            if request["method"] == "act":
                await asyncio.sleep(3)
            await websocket.send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
            )

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def fake_serial_slow_worker() -> AsyncIterator[str]:
    """Run a worker that serializes observe behind one deliberately stuck action."""

    request_lock = asyncio.Lock()

    async def handler(websocket) -> None:
        async for raw_message in websocket:
            request = json.loads(raw_message)
            request_id = request["id"]
            async with request_lock:
                if request["method"] == "act":
                    await asyncio.sleep(2.3)
                await websocket.send(
                    json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
                )

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()
