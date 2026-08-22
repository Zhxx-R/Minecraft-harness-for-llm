# Mineflayer Operation Guide

The LLM should never receive raw Mineflayer JavaScript APIs during normal task execution.

The harness exposes validated actions:

- `scan_blocks`: locate relevant loaded blocks by canonical block id.
- `scan_dropped_items`: locate nearby dropped item entities.
- `move_to`: approach a concrete coordinate.
- `dig_block_at`: dig one specific block coordinate.
- `wait_ticks`: wait briefly so vanilla Minecraft updates block drops, pickup, or other short-lived state.
- `query_inventory`: inspect inventory.
- `craft_item`: craft a canonical Minecraft item ID.
- `place_block`: place an inventory block.
- `use_item`: activate an item, block, or entity.
- `fight_entity`: attack a nearby entity if the task allows combat.
- `request_visual_snapshot`: request an image frame only when structured observations are insufficient.

The worker can use Mineflayer ecosystem libraries internally, but the action schema is the public runtime contract.
