import { startRpcServer } from "./rpc/server.js";

/** Port used by the worker WebSocket server. */
const port = Number(process.env.MINEFLAYER_WORKER_PORT ?? 8765);

startRpcServer({ port });
