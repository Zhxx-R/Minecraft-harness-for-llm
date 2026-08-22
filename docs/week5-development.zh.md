# Week 5 开发文档

## 目标

Week 5 把 Mineflayer worker 从“观察 + 窄范围挖掘”扩展成可用的高层 Minecraft 动作层。LLM 仍然不能调用 raw Mineflayer JavaScript，只能请求经过 harness 校验的动作，由 worker 在内部映射到 Mineflayer 调用。

## 已交付变更

- 扩展 worker actions：
  - `move_to`
  - `mine_block`
  - `craft_item`
  - `place_block`
  - `use_item`
  - `fight_entity`
  - 既有 `query_inventory`
  - 既有 `request_visual_snapshot`
- 统一 worker action result：
  - `ok`
  - `action_type`
  - `error_code`
  - `message`
  - `recoverable`
  - `observation`
- 为长时间 worker action 增加 timeout。
- 新增 backend `DEFAULT_WEEK5_ACTIONS`。
- 实现 `ProgrammaticVerifier` 基础检查：
  - `inventory_contains`
  - `block_placed`
  - `entity_defeated`
  - composite `all` / `any`
- 增加 verifier 和 Week 5 action scope 单测。
- 将 `vec3@0.1.10` 声明为 worker 直接依赖，和 Mineflayer 坐标类型保持一致。

## 动作语义

`move_to` 以直线方式移动到附近坐标：

```json
{"type":"move_to","args":{"position":{"x":10,"y":64,"z":10},"tolerance":1.5}}
```

`mine_block` 按 Mineflayer block name 查找并挖掘附近方块：

```json
{"type":"mine_block","args":{"block":"oak_log","count":1,"max_distance":6}}
```

`craft_item` 使用 inventory 或附近 crafting table 合成物品：

```json
{"type":"craft_item","args":{"item":"oak_planks","count":4}}
```

```json
{"type":"craft_item","args":{"item":"wooden_pickaxe","count":1,"station":"crafting_table"}}
```

这里的 `count` 表示希望得到的目标产物数量，不是底层 Mineflayer recipe 执行次数。例如 `oak_planks` 单次 recipe 产出 4 个，所以 `count: 4` 只会执行 1 次合成。

`place_block` 装备 inventory 中的方块，并放置到目标位置或 bot 脚下方块上方：

```json
{"type":"place_block","args":{"item":"crafting_table","position":{"x":11,"y":64,"z":10}}}
```

`use_item` 激活物品、附近方块或附近实体：

```json
{"type":"use_item","args":{"item":"wooden_pickaxe"}}
```

`fight_entity` 使用可选武器攻击附近实体：

```json
{"type":"fight_entity","args":{"entity":"zombie","weapon":"wooden_sword","max_attacks":5}}
```

## 错误分类

Worker 尽量把失败作为结构化数据返回，而不是让进程崩溃：

- `invalid_args`：action 参数不合法，模型应修复输出。
- `target_not_found`：目标 block/entity 不在附近，可通过移动或重规划恢复。
- `drop_not_collected`：方块已被挖掉，但没有掉落物进入 inventory，通常需要检查 bot 是否在 survival mode、是否靠近掉落物、背包是否已满。
- `missing_item`：inventory 缺少所需物品，需要先采集或合成。
- `missing_station`：附近缺少所需合成站。
- `recipe_not_available`：当前 inventory/station 无法合成目标物品。
- `not_diggable`：目标方块当前不可挖。
- `no_support_block`：放置目标没有可依附的相邻支撑方块。
- `entity_still_present`：攻击预算内没有击败目标。
- `timeout`：动作超过 timeout。
- `runtime_error`：Mineflayer 抛出未预期错误。

## 当前边界

Week 5 动作层是高层、可审计的 MVP，不是完整 Minecraft 自动化：

- `move_to` 是直线移动，不是完整 pathfinding。
- `mine_block` 会尝试靠近掉落物，但尚未使用 `collectBlock`。
- `craft_item` 依赖 Mineflayer 在当前 inventory/station 下可用的 recipes。
- `place_block` 需要显式目标支撑，或使用脚下方块作为简单放置位置。
- `fight_entity` 使用重复攻击，不包含复杂战斗策略。

后续可以把 pathfinder 和 collectBlock 作为 worker 内部实现加入，但不需要扩大 LLM 可见的 action API。

## 验证

自动检查：

```bash
make validate-schemas
make test-python
cd workers/mineflayer-worker && npm run typecheck
```

期望结果：

- Shared JSON schemas 通过。
- Backend tests 通过，包括 verifier 检查。
- Worker TypeScript typecheck 通过。

实机 smoke test：

```bash
./scripts/dev-worker.sh
```

另开一个终端：

```bash
backend/.venv/bin/python scripts/smoke_week5_actions.py \
  --port 52025 \
  --username Week5Harness \
  --pre-action-delay-sec 30 \
  --hold-open-sec 60
```

脚本会在 bot 进入游戏后等待 `--pre-action-delay-sec` 秒。等待期间可以在 Minecraft 聊天框执行：

```text
/tp Week5Harness 你的玩家名
```

然后在 bot 旁边 8 格内放置至少 3 个 `oak_log`。脚本会继续执行 `mine_block oak_log x3 -> craft_item oak_planks x12 -> craft_item crafting_table -> place_block -> craft_item stick -> craft_item wooden_pickaxe` 动作链，并把完整结果写入：

```text
runs/week5_live_actions.json
```

如果希望跑完后 bot 继续长时间在线观察，可以把 `--hold-open-sec` 设大，或添加 `--keep-open`。

如果 `mine_block` 返回 `target_not_found`，说明 action 层正常返回了结构化失败，但 bot 附近没有可挖的 `oak_log`。

如果 `mine_block` 返回 `drop_not_collected`，说明 bot 挖掉了方块但没有拿到掉落物。优先在 Minecraft 聊天框确认：

```text
/gamemode survival Week5Harness
```
