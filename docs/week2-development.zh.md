# Week 2 开发文档

## 目标

Week 2 实现第一个真实 runtime 边界：FastAPI backend 可以通过 WebSocket JSON-RPC 与 Node.js Mineflayer worker 通信；worker 可以连接 Minecraft server，暴露结构化 observation，执行基础动作，并上报 lifecycle events。

## 已交付变更

- 实现 worker JSON-RPC 2.0 子集：
  - `reset`
  - `observe`
  - `act`
  - `snapshot`
  - `close`
- 通过 `worker.event` 上报 worker lifecycle notifications：
  - `connected`
  - `bot_connecting`
  - `spawned`
  - `bot_disconnected`
  - `kicked`
  - `error`
  - `timeout`
  - `closed`
- 新增 backend `MineflayerClient`：
  - 维护单个 WebSocket 连接。
  - 发送 request/response JSON-RPC 调用。
  - 缓存 lifecycle notifications，供审计和测试使用。
  - worker 返回错误时抛出 `MineflayerRpcError`。
- 扩展结构化 observation：
  - position
  - health
  - food
  - inventory
  - nearby entities
  - nearby blocks
- 实现 Week 2 基础 worker actions：
  - `query_inventory`
  - `request_visual_snapshot`
- 新增 fake JSON-RPC worker 的 backend contract tests。
- 新增 `scripts/smoke_mineflayer_rpc.py`，用于对 live worker 和 Minecraft server 做手动验证。

## 协议形态

Backend request：

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "method": "observe",
  "params": {}
}
```

Worker success response：

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

Worker lifecycle notification：

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

## 手动 Smoke Test

先在本地启动 Minecraft server，监听 `localhost:25565`，然后启动 worker：

```bash
cd workers/mineflayer-worker
npm run dev
```

另开一个终端执行：

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
$PYTHON scripts/smoke_mineflayer_rpc.py \
  --worker-url ws://localhost:8765 \
  --host localhost \
  --port 52129 \
  --username HarnessAgent
```

期望结果：

- `reset` 等待 Mineflayer 触发 `spawn`。
- `observe` 返回 position/health/food/inventory/nearby world state。
- `act(query_inventory)` 返回 inventory contents。
- `snapshot` 返回带有当前 observation 的视觉占位结果。
- lifecycle events 至少包含 `connected`、`bot_connecting`、`spawned`、`closed`。

## 当前边界

- `request_visual_snapshot` 还不抓取真实像素，只返回结构化 observation 和占位说明。
- 当前只实现 `query_inventory` 和 `request_visual_snapshot` 两个动作。Minecraft 动作能力扩展在 Week 5。
- worker `reset` 会等待 `spawn`，超时时间来自 `MINECRAFT_SPAWN_TIMEOUT_MS` 或 request 中的 `runtime.spawn_timeout_ms`。
- lifecycle events 当前由 `MineflayerClient` 保存在内存中；持久化审计从 Week 4 开始。

## 验证

自动检查：

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make ci
```

当前结果：

- Shared schema validation passed：3 个 schemas。
- Backend tests passed：6 个 tests。
- Worker TypeScript typecheck passed。
- Frontend TypeScript typecheck passed。

