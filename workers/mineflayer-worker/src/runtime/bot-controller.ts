import mineflayer, { type Bot } from "mineflayer";
import mineflayerPathfinder from "mineflayer-pathfinder";
import { trackSpawnSequence } from "./spawn-sequence.js";

const { pathfinder } = mineflayerPathfinder;

/** Connection settings required to create one Mineflayer bot instance. */
export interface BotConnectionOptions {
  host: string;
  port: number;
  username: string;
}

/** Owns the active Mineflayer bot lifecycle for the worker process. */
export class BotController {
  private bot: Bot | null = null;

  /** Create and retain a Mineflayer bot connection. */
  connect(options: BotConnectionOptions): Bot {
    this.close();
    this.bot = mineflayer.createBot(options);
    trackSpawnSequence(this.bot);
    this.bot.loadPlugin(pathfinder);
    return this.bot;
  }

  /** Return the current bot or fail if the worker has not connected yet. */
  current(): Bot {
    if (!this.bot) {
      throw new Error("Bot is not connected.");
    }
    return this.bot;
  }

  /** Close the active Mineflayer bot connection if one exists. */
  close(): void {
    if (!this.bot) {
      return;
    }

    if (typeof this.bot.quit === "function") {
      this.bot.quit("Harness worker reset or close requested.");
    } else if (typeof this.bot.end === "function") {
      this.bot.end("Harness worker reset or close requested.");
    }
    this.bot = null;
  }

  /** Report whether a bot connection has been created. */
  isConnected(): boolean {
    return this.bot !== null;
  }
}
