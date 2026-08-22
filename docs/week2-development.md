# Week 2 Development Document

## Goal

Week 2 implements the first real runtime boundary: the FastAPI backend can talk to the Node.js Mineflayer worker over WebSocket JSON-RPC, and the worker can connect to a Minecraft server, expose structured observations, execute base actions, and emit lifecycle events.

## Delivered Changes

- Implemented worker JSON-RPC 2.0 subset:
  - `reset`
  - `observe`
  - `act`
  - `snapshot`
  - `close`
- Added worker lifecycle notifications through `worker.event`:
  - `connected`
  - `bot_connecting`
  - `spawned`
  - `bot_disconnected`
  - `kicked`
  - `error`
  - `timeout`
  - `closed`
- Added backend `MineflayerClient`:
  - Maintains one WebSocket connection.
  - Sends request/response JSON-RPC calls.
  - Stores lifecycle notifications for audit and tests.
  - Raises `MineflayerRpcError` on worker errors.
- Expanded structured observation:
  - position
  - health
  - food
  - inventory
  - nearby entities
  - nearby blocks
- Implemented Week 2 base worker actions:
  - `query_inventory`
  - `request_visual_snapshot`
- Added backend contract tests with a fake JSON-RPC worker.
- Added `scripts/smoke_mineflayer_rpc.py` for manual testing against a live worker and Minecraft server.

## Protocol Shape

Backend request:

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "method": "observe",
  "params": {}
}
```

Worker success response:

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "result": {
    "health": 20,
    "food": 20,
    "inventory": []
  }
}
```

Worker lifecycle notification:

```json
{
  "jsonrpc": "2.0",
  "method": "worker.event",
  "params": {
    "event": "spawned",
    "timestamp": "2026-06-22T00:00:00.000Z",
    "payload": {
      "username": "HarnessAgent"
    }
  }
}
```

## Manual Smoke Test

Start a Minecraft server locally on `localhost:25565`, then start the worker:

```bash
cd workers/mineflayer-worker
npm run dev
```

In another terminal, run:

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
$PYTHON scripts/smoke_mineflayer_rpc.py \
  --worker-url ws://localhost:8765 \
  --host localhost \
  --port 25565 \
  --username HarnessAgent
```

Expected result:

- `reset` waits until Mineflayer emits `spawn`.
- `observe` returns structured position/health/food/inventory/world-nearby state.
- `act(query_inventory)` returns inventory contents.
- `snapshot` returns a placeholder visual snapshot with current observation.
- lifecycle events include at least `connected`, `bot_connecting`, `spawned`, and `closed`.

## Current Boundaries

- `request_visual_snapshot` does not capture pixels yet; it returns a placeholder with structured observation.
- Only `query_inventory` and `request_visual_snapshot` are implemented actions. Week 5 expands Minecraft action capability.
- Worker `reset` waits for `spawn` and times out after `MINECRAFT_SPAWN_TIMEOUT_MS` or request value `runtime.spawn_timeout_ms`.
- Lifecycle events are held in memory by `MineflayerClient`; persistent audit storage starts in Week 4.

## Verification

Automated checks:

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make ci
```

Current result:

- Shared schema validation passed: 3 schemas.
- Backend tests passed: 6 tests.
- Worker TypeScript typecheck passed.
- Frontend TypeScript typecheck passed.

