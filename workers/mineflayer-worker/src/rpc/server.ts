import { WebSocketServer } from "ws";
import type { WebSocket } from "ws";
import type { Bot } from "mineflayer";
import { harnessActionSchema } from "../actions/action-schemas.js";
import { handleAction } from "../actions/action-handlers.js";
import { observe } from "../observations/observe.js";
import { BotController } from "../runtime/bot-controller.js";
import type {
  JsonValue,
  RpcErrorResponse,
  RpcNotification,
  RpcRequest,
  RpcResponse,
  RpcSuccessResponse
} from "./types.js";

/** Start the worker RPC server used by the backend harness. */
export function startRpcServer(options: { port: number }) {
  const server = new WebSocketServer({ port: options.port });
  const controller = new BotController();

  server.on("connection", (socket) => {
    emitLifecycle(socket, "connected", { worker: "mineflayer-worker" });

    socket.on("message", async (rawMessage) => {
      const response = await handleRpcMessage(rawMessage.toString(), controller, socket);
      socket.send(JSON.stringify(response));
    });

    socket.on("close", () => {
      emitLifecycle(socket, "disconnected", { worker: "mineflayer-worker" });
    });
  });

  console.log(`Mineflayer worker listening on ws://localhost:${options.port}`);
  return server;
}

/** Handle one JSON-RPC request payload from the backend harness. */
async function handleRpcMessage(
  rawMessage: string,
  controller: BotController,
  socket: WebSocket
): Promise<RpcResponse> {
  let request: RpcRequest;
  try {
    request = parseRpcRequest(rawMessage);
  } catch (error) {
    return errorResponse(null, -32700, "Invalid JSON-RPC request.", { detail: errorToString(error) });
  }

  try {
    const result = await dispatchRpcRequest(request, controller, socket);
    return successResponse(request.id, result);
  } catch (error) {
    emitLifecycle(socket, "error", { detail: errorToString(error), method: request.method });
    return errorResponse(request.id, -32000, errorToString(error));
  }
}

/** Dispatch a parsed request to the worker method implementation. */
async function dispatchRpcRequest(
  request: RpcRequest,
  controller: BotController,
  socket: WebSocket
): Promise<JsonValue> {
  switch (request.method) {
    case "reset":
      return await resetBot(request.params ?? {}, controller, socket);
    case "observe":
      return observe(controller.current()) as JsonValue;
    case "act": {
      const action = harnessActionSchema.parse(request.params?.action);
      return (await handleAction(controller.current(), action)) as JsonValue;
    }
    case "snapshot":
      return {
        image: null,
        format: null,
        reason: "Visual frame capture is not configured in the Week 2 worker.",
        observation: observe(controller.current())
      } as JsonValue;
    case "close":
      controller.close();
      emitLifecycle(socket, "closed", { worker: "mineflayer-worker" });
      return { ok: true };
    default:
      return unreachableMethod(request.method);
  }
}

/** Create a new Mineflayer connection using request params or environment defaults. */
async function resetBot(
  params: Record<string, JsonValue>,
  controller: BotController,
  socket: WebSocket
): Promise<JsonValue> {
  const runtime = (params.runtime ?? {}) as Record<string, JsonValue>;
  const host = stringParam(runtime.host, process.env.MINECRAFT_HOST ?? "localhost");
  const port = numberParam(runtime.port, Number(process.env.MINECRAFT_PORT ?? 25565));
  const username = stringParam(runtime.username, process.env.MINECRAFT_USERNAME ?? "HarnessAgent");
  const spawnTimeoutMs = numberParam(
    runtime.spawn_timeout_ms,
    Number(process.env.MINECRAFT_SPAWN_TIMEOUT_MS ?? 15000)
  );

  const bot = controller.connect({ host, port, username });
  emitLifecycle(socket, "bot_connecting", { host, port, username });

  bot.once("spawn", () => {
    emitLifecycle(socket, "spawned", { username });
  });
  bot.once("end", (reason) => {
    emitLifecycle(socket, "bot_disconnected", { reason: String(reason ?? "unknown") });
  });
  bot.once("kicked", (reason) => {
    emitLifecycle(socket, "kicked", { reason: String(reason) });
  });
  bot.once("error", (error) => {
    emitLifecycle(socket, "error", { detail: errorToString(error) });
  });

  await waitForSpawn(bot, spawnTimeoutMs, socket);
  const resetPolicy = (runtime.reset_policy ?? {}) as Record<string, JsonValue>;
  const resetPolicyResult = await applyResetPolicy(bot, username, resetPolicy, socket);
  return { ok: true, host, port, username, reset_policy: resetPolicyResult };
}

/** Apply optional environment reset operations after the bot has spawned. */
async function applyResetPolicy(
  bot: Bot,
  username: string,
  resetPolicy: Record<string, JsonValue>,
  socket: WebSocket
): Promise<Record<string, JsonValue>> {
  const clearInventory = (resetPolicy.clear_inventory ?? {}) as Record<string, JsonValue>;
  const clearResult = await clearInventoryForReset(bot, username, clearInventory, socket);
  return { clear_inventory: clearResult };
}

/** Clear selected inventory items using server commands so drops do not pollute verifier state. */
async function clearInventoryForReset(
  bot: Bot,
  username: string,
  config: Record<string, JsonValue>,
  socket: WebSocket
): Promise<Record<string, JsonValue>> {
  const enabled = booleanParam(config.enabled, false);
  const mode = stringParam(config.mode, "items");
  const waitMs = numberParam(config.wait_ms, 750);
  const commandFeedbackWaitMs = numberParam(config.command_feedback_wait_ms, 500);
  const dropFallback = booleanParam(config.drop_fallback, true);
  const items = stringArrayParam(config.items);
  const before = inventorySnapshot(bot);
  if (!enabled) {
    return {
      enabled: false,
      mode,
      items,
      before,
      after: before,
      commands: [],
      command_feedback: [],
      fallback: { enabled: false, reason: "clear_inventory_disabled" },
      verified: true
    };
  }

  const commands =
    mode === "all"
      ? [`/clear ${username}`]
      : items.map((item) => `/clear ${username} ${minecraftId(item)}`);
  const commandFeedback: Record<string, JsonValue>[] = [];
  for (const command of commands) {
    emitLifecycle(socket, "reset_clear_inventory_command", { username, command });
    const feedback = await sendCommandWithFeedback(bot, command, commandFeedbackWaitMs);
    commandFeedback.push({ command, messages: feedback });
    await sleep(100);
  }
  await sleep(waitMs);
  let after = inventorySnapshot(bot);
  let verified = isClearVerified(after, mode, items);
  let fallback: Record<string, JsonValue> = {
    enabled: false,
    reason: verified ? "command_clear_verified" : "command_clear_unverified"
  };
  if (!verified && dropFallback) {
    fallback = await dropInventoryForReset(bot, mode, items, socket);
    await sleep(waitMs);
    after = inventorySnapshot(bot);
    verified = isClearVerified(after, mode, items);
  }
  emitLifecycle(socket, "reset_clear_inventory", {
    username,
    mode,
    items,
    commands,
    command_feedback: commandFeedback,
    fallback,
    verified,
    before,
    after
  });
  return {
    enabled: true,
    mode,
    items,
    before,
    after,
    commands,
    command_feedback: commandFeedback,
    fallback,
    verified,
    wait_ms: waitMs
  };
}

/** Send one server command through chat and collect short server feedback messages. */
async function sendCommandWithFeedback(bot: Bot, command: string, waitMs: number): Promise<string[]> {
  const messages: string[] = [];
  const onMessage = (message: string) => {
    messages.push(message);
  };

  bot.on("messagestr", onMessage);
  try {
    bot.chat(command);
    await sleep(waitMs);
  } finally {
    bot.removeListener("messagestr", onMessage);
  }
  return messages;
}

/** Drop inventory items client-side when the bot lacks permission to run /clear. */
async function dropInventoryForReset(
  bot: Bot,
  mode: string,
  items: string[],
  socket: WebSocket
): Promise<Record<string, JsonValue>> {
  const before = inventorySnapshot(bot);
  const targets = new Set(items.map((item) => stripMinecraftNamespace(item)));
  const dropped: Record<string, JsonValue>[] = [];
  const errors: Record<string, JsonValue>[] = [];
  const inventoryItems = [...bot.inventory.items()];

  for (const item of inventoryItems) {
    if (mode !== "all" && !targets.has(stripMinecraftNamespace(item.name))) {
      continue;
    }
    const droppedItem = { name: item.name, count: item.count };
    try {
      emitLifecycle(socket, "reset_drop_inventory_item", droppedItem);
      await bot.tossStack(item);
      dropped.push(droppedItem);
      await sleep(100);
    } catch (error) {
      errors.push({ ...droppedItem, error: errorToString(error) });
    }
  }

  const after = inventorySnapshot(bot);
  return {
    enabled: true,
    reason: "command_clear_unverified",
    before,
    after,
    dropped,
    errors,
    verified: isClearVerified(after, mode, items),
    warning: "Dropped items remain in the world unless server-side item cleanup is available."
  };
}

/** Wait for Mineflayer spawn so observe calls do not race connection startup. */
function waitForSpawn(bot: Bot, timeoutMs: number, socket: WebSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup();
      emitLifecycle(socket, "timeout", { phase: "spawn", timeout_ms: timeoutMs });
      reject(new Error(`Timed out waiting for Mineflayer spawn after ${timeoutMs}ms.`));
    }, timeoutMs);

    const cleanup = () => {
      clearTimeout(timeout);
      bot.removeListener("spawn", onSpawn);
      bot.removeListener("error", onError);
      bot.removeListener("kicked", onKicked);
      bot.removeListener("end", onEnd);
    };

    const onSpawn = () => {
      cleanup();
      resolve();
    };
    const onError = (error: Error) => {
      cleanup();
      reject(error);
    };
    const onKicked = (reason: string) => {
      cleanup();
      reject(new Error(`Mineflayer bot was kicked before spawn: ${reason}`));
    };
    const onEnd = (reason: string) => {
      cleanup();
      reject(new Error(`Mineflayer bot disconnected before spawn: ${reason}`));
    };

    bot.once("spawn", onSpawn);
    bot.once("error", onError);
    bot.once("kicked", onKicked);
    bot.once("end", onEnd);
  });
}

/** Parse and minimally validate one JSON-RPC request. */
function parseRpcRequest(rawMessage: string): RpcRequest {
  const parsed = JSON.parse(rawMessage) as Partial<RpcRequest>;
  if (parsed.jsonrpc !== "2.0" || parsed.id === undefined || typeof parsed.method !== "string") {
    throw new Error("Expected JSON-RPC 2.0 request with id and method.");
  }
  return parsed as RpcRequest;
}

/** Build one successful JSON-RPC response. */
function successResponse(id: string | number, result: JsonValue): RpcSuccessResponse {
  return { jsonrpc: "2.0", id, result };
}

/** Build one failed JSON-RPC response. */
function errorResponse(
  id: string | number | null,
  code: number,
  message: string,
  data?: JsonValue
): RpcErrorResponse {
  return { jsonrpc: "2.0", id, error: { code, message, data } };
}

/** Emit a lifecycle notification without interrupting request processing. */
function emitLifecycle(socket: WebSocket, event: string, payload: Record<string, JsonValue> = {}): void {
  if (socket.readyState !== socket.OPEN) {
    return;
  }

  const notification: RpcNotification = {
    jsonrpc: "2.0",
    method: "worker.event",
    params: {
      event,
      payload,
      timestamp: new Date().toISOString()
    }
  };
  socket.send(JSON.stringify(notification));
}

/** Convert a string-like JSON value to a string with a fallback. */
function stringParam(value: JsonValue | undefined, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

/** Convert a numeric JSON value to a number with a fallback. */
function numberParam(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}

/** Convert a boolean-like JSON value to a boolean with a fallback. */
function booleanParam(value: JsonValue | undefined, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/** Convert a JSON array value to a list of strings. */
function stringArrayParam(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

/** Return a compact inventory snapshot for reset audit. */
function inventorySnapshot(bot: Bot): Record<string, JsonValue>[] {
  return bot.inventory.items().map((item) => ({
    name: item.name,
    count: item.count
  }));
}

/** Return whether all configured target items are absent from inventory. */
function targetItemsCleared(inventory: Record<string, JsonValue>[], items: string[]): boolean {
  const targets = new Set(items.map((item) => stripMinecraftNamespace(item)));
  if (targets.size === 0) {
    return true;
  }
  return inventory.every((item) => {
    const name = item.name;
    return typeof name !== "string" || !targets.has(stripMinecraftNamespace(name));
  });
}

/** Return whether the configured clear mode has removed the requested inventory items. */
function isClearVerified(inventory: Record<string, JsonValue>[], mode: string, items: string[]): boolean {
  return mode === "all" ? inventory.length === 0 : targetItemsCleared(inventory, items);
}

/** Add the minecraft namespace for server commands when the id is unqualified. */
function minecraftId(item: string): string {
  return item.includes(":") ? item : `minecraft:${item}`;
}

/** Remove the minecraft namespace for comparing Mineflayer inventory names. */
function stripMinecraftNamespace(item: string): string {
  return item.startsWith("minecraft:") ? item.slice("minecraft:".length) : item;
}

/** Sleep for a small reset synchronization window. */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Normalize thrown values into response-safe error strings. */
function errorToString(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Preserve exhaustiveness checking for RPC method dispatch. */
function unreachableMethod(method: never): never {
  throw new Error(`Unsupported RPC method: ${method}`);
}
