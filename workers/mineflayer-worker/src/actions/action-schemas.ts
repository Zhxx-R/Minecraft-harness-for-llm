import { z } from "zod";

/** Runtime action schema shared with the backend harness action contract. */
export const harnessActionSchema = z.object({
  type: z.enum([
    "scan_blocks",
    "scan_entities",
    "scan_dropped_items",
    "move_to",
    "follow",
    "dig_block_at",
    "wait_ticks",
    "process_item",
    "craft_item",
    "smelt_item",
    "place_block",
    "equip_item",
    "fight_entity",
    "use_item",
    "consume_item",
    "move_to_and_engage_combat",
    "engage_combat",
    "query_inventory",
    "execute_skill",
    "request_visual_snapshot"
  ]),
  args: z.record(z.unknown()).default({})
});

/** Validated high-level action accepted by the Mineflayer worker. */
export type HarnessAction = z.infer<typeof harnessActionSchema>;
