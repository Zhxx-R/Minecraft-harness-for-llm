from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mc_agent_harness.schemas.action import HarnessAction
from websockets.asyncio.client import ClientConnection, connect


DEFAULT_ACTION_TIMEOUT_MS = {
    "scan_blocks": 5000,
    "scan_entities": 3000,
    "scan_dropped_items": 3000,
    "move_to": 90000,
    "follow": 3000,
    "dig_block_at": 12000,
    "wait_ticks": 5000,
    "process_item": 90000,
    "craft_item": 15000,
    "smelt_item": 90000,
    "place_block": 12000,
    "equip_item": 8000,
    "use_item": 10000,
    "consume_item": 12000,
    "move_to_and_engage_combat": 50000,
    "engage_combat": 50000,
    "fight_entity": 17000,
    "query_inventory": 3000,
    "request_visual_snapshot": 5000,
}

# Extra seconds added to worker-side action timeout budgets for JSON-RPC round-trip overhead.
ACTION_RPC_TIMEOUT_BUFFER_SEC = 2.0

# A short independent observe call distinguishes a busy action from a dead worker process.
ACTION_TIMEOUT_HEALTH_PROBE_SEC = 2.0


@dataclass(frozen=True, slots=True)
class WorkerLifecycleEvent:
    """Lifecycle notification emitted by the Mineflayer worker."""

    event: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)


class MineflayerRpcError(RuntimeError):
    """Raised when the Mineflayer worker returns a JSON-RPC error response."""


class MineflayerClient:
    """Client for the Node.js Mineflayer worker."""

    def __init__(self, worker_url: str, request_timeout: float = 10.0) -> None:
        self.worker_url = worker_url
        self.request_timeout = request_timeout
        self._connection: ClientConnection | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._next_request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._lifecycle_events: list[WorkerLifecycleEvent] = []
        self._last_reset_result: dict[str, Any] | None = None

    @property
    def lifecycle_events(self) -> tuple[WorkerLifecycleEvent, ...]:
        """Return lifecycle notifications received from the worker."""

        return tuple(self._lifecycle_events)

    @property
    def last_reset_result(self) -> dict[str, Any] | None:
        """Return the latest worker reset metadata for audit."""

        return self._last_reset_result

    async def connect(self) -> None:
        """Open the worker WebSocket connection if needed."""

        if self._connection is not None:
            return

        self._connection = await connect(self.worker_url, proxy=None)
        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Ask the worker to reset for a task specification."""

        self._last_reset_result = await self._request("reset", task_spec)
        return self._last_reset_result

    async def observe(self) -> dict[str, Any]:
        """Read structured observation from the Mineflayer worker."""

        return await self._request("observe", {})

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Dispatch one validated action to the Mineflayer worker."""

        timeout = _action_request_timeout(action, self.request_timeout)
        try:
            return await self._request(
                "act",
                {"action": action.model_dump()},
                timeout=timeout,
            )
        except TimeoutError:
            health = await self._probe_after_action_timeout()
            payload = {
                "ok": False,
                "action_type": action.type,
                "error_code": "rpc_timeout",
                "message": f"Mineflayer worker did not return action result within {timeout:.3f}s.",
                "recoverable": health["responsive"],
                "terminated": not health["responsive"],
                "requires_worker_restart": True,
                "worker_health": health,
            }
            if isinstance(health.get("observation"), dict):
                payload["observation"] = health["observation"]
            self._lifecycle_events.append(
                WorkerLifecycleEvent(
                    event="action_rpc_timeout",
                    timestamp=datetime.now(UTC).isoformat(),
                    payload={
                        "action_type": action.type,
                        "timeout_sec": timeout,
                        "worker_health": health,
                    },
                )
            )
            return payload

    async def snapshot(self) -> dict[str, Any]:
        """Request a runtime snapshot from the Mineflayer worker."""

        return await self._request("snapshot", {})

    async def _probe_after_action_timeout(self) -> dict[str, Any]:
        """Probe worker responsiveness without assuming the timed-out action completed."""

        started = asyncio.get_running_loop().time()
        try:
            observation = await self._request(
                "observe",
                {},
                timeout=ACTION_TIMEOUT_HEALTH_PROBE_SEC,
            )
        except Exception as exc:  # noqa: BLE001 - probe evidence belongs in the action result.
            return {
                "responsive": False,
                "probe_method": "observe",
                "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000, 3),
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        return {
            "responsive": True,
            "probe_method": "observe",
            "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000, 3),
            "observation": observation,
        }

    async def close(self) -> None:
        """Close the worker client connection."""

        if self._connection is None:
            return

        try:
            await asyncio.wait_for(
                self._request("close", {}),
                timeout=min(self.request_timeout, 3.0),
            )
        except Exception:
            pass
        finally:
            await self._shutdown()

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request and wait for its matching response."""

        await self.connect()
        assert self._connection is not None

        request_id = self._next_request_id
        self._next_request_id += 1

        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = response_future

        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        await self._connection.send(json.dumps(payload))

        try:
            response = await asyncio.wait_for(
                response_future,
                timeout=timeout or self.request_timeout,
            )
        finally:
            self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            message = error.get("message", "Mineflayer worker RPC failed.")
            raise MineflayerRpcError(str(message))

        result = response.get("result")
        if not isinstance(result, dict):
            raise MineflayerRpcError("Mineflayer worker returned a non-object result.")
        return result

    async def _receive_loop(self) -> None:
        """Route JSON-RPC responses and lifecycle notifications from the worker."""

        assert self._connection is not None
        failure: BaseException | None = None
        try:
            async for raw_message in self._connection:
                message = json.loads(raw_message)
                if message.get("method") == "worker.event":
                    self._record_lifecycle_event(message.get("params", {}))
                    continue

                response_id = message.get("id")
                if isinstance(response_id, int) and response_id in self._pending:
                    future = self._pending[response_id]
                    if not future.done():
                        future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        else:
            failure = MineflayerRpcError("Mineflayer worker connection closed.")

        for future in self._pending.values():
            if not future.done() and failure is not None:
                future.set_exception(failure)

    def _record_lifecycle_event(self, params: dict[str, Any]) -> None:
        """Store one worker lifecycle notification for audit or tests."""

        event = params.get("event")
        timestamp = params.get("timestamp")
        payload = params.get("payload", {})
        if isinstance(event, str) and isinstance(timestamp, str) and isinstance(payload, dict):
            self._lifecycle_events.append(
                WorkerLifecycleEvent(event=event, timestamp=timestamp, payload=payload)
            )

    async def _shutdown(self) -> None:
        """Close local websocket state and cancel the receiver task."""

        connection = self._connection
        self._connection = None

        if connection is not None:
            await connection.close()

        if self._receiver_task is not None:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None


def _action_request_timeout(action: HarnessAction, request_timeout: float) -> float:
    """Return the backend RPC watchdog timeout for one worker action."""

    configured_timeout_ms = action.args.get("timeout_ms")
    if not isinstance(configured_timeout_ms, (int, float)) or configured_timeout_ms <= 0:
        configured_timeout_ms = DEFAULT_ACTION_TIMEOUT_MS.get(action.type)
    if not isinstance(configured_timeout_ms, (int, float)) or configured_timeout_ms <= 0:
        return request_timeout
    action_timeout = (float(configured_timeout_ms) / 1000.0) + ACTION_RPC_TIMEOUT_BUFFER_SEC
    return max(1.0, action_timeout)
