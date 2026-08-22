/** JSON value supported by the worker RPC protocol. */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

/** Request sent by the backend harness to the Mineflayer worker. */
export interface RpcRequest {
  jsonrpc: "2.0";
  id: string | number;
  method: "reset" | "observe" | "act" | "snapshot" | "close";
  params?: Record<string, JsonValue>;
}

/** Successful response sent by the Mineflayer worker. */
export interface RpcSuccessResponse {
  jsonrpc: "2.0";
  id: string | number;
  result: JsonValue;
}

/** Error response sent by the Mineflayer worker. */
export interface RpcErrorResponse {
  jsonrpc: "2.0";
  id: string | number | null;
  error: {
    code: number;
    message: string;
    data?: JsonValue;
  };
}

/** Lifecycle notification emitted by the Mineflayer worker. */
export interface RpcNotification {
  jsonrpc: "2.0";
  method: "worker.event";
  params: {
    event: string;
    payload?: Record<string, JsonValue>;
    timestamp: string;
  };
}

/** Response variants accepted by the backend harness. */
export type RpcResponse = RpcSuccessResponse | RpcErrorResponse;

