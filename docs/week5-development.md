# Week 5 Development Document

## Goal

Week 5 expands the Mineflayer worker from inspection and narrow mining into a practical high-level Minecraft action layer. The LLM still cannot call raw Mineflayer JavaScript. It can only request validated harness actions that the worker maps to Mineflayer calls.

## Delivered Changes

- Expanded worker actions:
  - `move_to`
  - `mine_block`
  - `craft_item`
  - `place_block`
  - `use_item`
  - `fight_entity`
  - existing `query_inventory`
  - existing `request_visual_snapshot`
- Added normalized worker action results:
  - `ok`
  - `action_type`
  - `error_code`
  - `message`
  - `recoverable`
  - `observation`
- Added action timeouts for long-running worker actions.
- Added backend `DEFAULT_WEEK5_ACTIONS`.
- Implemented `ProgrammaticVerifier` checks:
  - `inventory_contains`
  - `block_placed`
  - `entity_defeated`
  - composite `all` / `any`
- Added unit tests for verifier behavior and Week 5 action scope.
- Declared `vec3@0.1.10` as a direct worker dependency to match Mineflayer's coordinate type.

## Action Semantics

`move_to` moves in a straight line to a nearby coordinate:

```json
{"type":"move_to","args":{"position":{"x":10,"y":64,"z":10},"tolerance":1.5}}
```

`mine_block` finds and digs nearby blocks by Mineflayer block name:

```json
{"type":"mine_block","args":{"block":"oak_log","count":1,"max_distance":6}}
```

`craft_item` crafts from inventory or a nearby crafting table:

```json
{"type":"craft_item","args":{"item":"oak_planks","count":4}}
```

```json
{"type":"craft_item","args":{"item":"wooden_pickaxe","count":1,"station":"crafting_table"}}
```

Here `count` means desired output item count, not the underlying Mineflayer recipe execution count. For example, one `oak_planks` recipe produces 4 planks, so `count: 4` runs one recipe.

`place_block` equips an inventory block and places it at a target or on top of the block below the bot:

```json
{"type":"place_block","args":{"item":"crafting_table","position":{"x":11,"y":64,"z":10}}}
```

`use_item` activates an item, nearby block, or nearby entity:

```json
{"type":"use_item","args":{"item":"wooden_pickaxe"}}
```

`fight_entity` attacks a nearby entity with an optional weapon:

```json
{"type":"fight_entity","args":{"entity":"zombie","weapon":"wooden_sword","max_attacks":5}}
```

## Error Taxonomy

Worker failures are returned as data instead of crashing the process whenever possible:

- `invalid_args`: action arguments are malformed and should be repaired by the model.
- `target_not_found`: requested block/entity is not nearby; recoverable by moving or replanning.
- `drop_not_collected`: the block was mined but no dropped item reached inventory; usually check survival mode, drop pickup distance, or inventory space.
- `missing_item`: inventory lacks the required item; recoverable by gathering/crafting first.
- `missing_station`: required crafting station is not nearby.
- `recipe_not_available`: current inventory/station cannot craft the item.
- `not_diggable`: target block cannot currently be dug.
- `no_support_block`: placement target has no valid adjacent support block.
- `entity_still_present`: combat did not defeat the target within attack budget.
- `timeout`: action exceeded its timeout.
- `runtime_error`: Mineflayer threw an unexpected error.

## Current Boundary

The Week 5 action layer is intentionally high-level and auditable, but not complete Minecraft automation:

- `move_to` is straight-line movement, not full pathfinding.
- `mine_block` can step toward drops but does not use `collectBlock`.
- `craft_item` relies on Mineflayer recipes available from current inventory and station.
- `place_block` requires either an explicit target with support or a simple below-bot placement.
- `fight_entity` uses repeated attacks but does not implement advanced combat tactics.

Pathfinder and collectBlock can be added later as worker-internal implementations without expanding the LLM-visible action API.

## Verification

Automated checks:

```bash
make validate-schemas
make test-python
cd workers/mineflayer-worker && npm run typecheck
```

Expected result:

- Shared JSON schemas pass.
- Backend tests pass, including verifier checks.
- Worker TypeScript typecheck passes.

Live smoke test:

```bash
./scripts/dev-worker.sh
```

In another terminal:

```bash
backend/.venv/bin/python scripts/smoke_week5_actions.py \
  --port 52025 \
  --username Week5Harness \
  --pre-action-delay-sec 30 \
  --hold-open-sec 60
```

The script keeps the bot online for `--pre-action-delay-sec` seconds before actions start. During that window, run this in Minecraft chat:

```text
/tp Week5Harness your_player_name
```

Then place at least 3 `oak_log` blocks within 8 blocks of the bot. The script continues with the `mine_block oak_log x3 -> craft_item oak_planks x12 -> craft_item crafting_table -> place_block -> craft_item stick -> craft_item wooden_pickaxe` action chain and writes the full report to:

```text
runs/week5_live_actions.json
```

To keep the bot online longer after the action plan, increase `--hold-open-sec` or add `--keep-open`.

If `mine_block` returns `target_not_found`, the action layer returned a structured failure correctly, but no mineable `oak_log` was near the bot.

If `mine_block` returns `drop_not_collected`, the bot mined the block but did not pick up the drop. First confirm in Minecraft chat:

```text
/gamemode survival Week5Harness
```
