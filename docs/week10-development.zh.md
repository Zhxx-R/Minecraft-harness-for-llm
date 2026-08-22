# Week 10 开发文档：Programmatic Catalog、差异化 Batch 与 Skill 去重

## 范围

Week 10 把 Week 9 的并行 runner 往 skill 训练系统推进了一步。本周新增：

- MineDojo 完整 1581 个 programmatic task 的本地 catalog snapshot。
- 从 MineDojo 官方 task description 文件导入 catalog 的脚本。
- 任务相似度 scorer 和 diversity-aware batch planner。
- Week 9 training runner 支持可选的 diverse batch selection。
- skill candidate 在 promotion 前的重复检测能力。
- live parallel programmatic training runner：可以启动多个 Mineflayer workers，执行可运行的 programmatic manifests，并更新 SQL skill library。
- Prompt 分层与 harvest 原子动作空间：让 agent 通过 `scan -> move -> dig -> collect -> verify` 学到 procedure skill，而不是依赖 worker 宏动作。

完整 catalog 不等于当前 CI 可执行任务集。Catalog 保存官方 task prompt/guidance/category，用于任务选择、分桶和后续 live training。`tasks/manifests/` 下的 curated manifests 仍然是 deterministic CI/smoke 集，因为它们包含 scripted action trace 和可执行 verifier。

## Week10C：MineDojo 全量 Programmatic 对齐

本阶段新增 executable manifest adapter，把本地 1581 条 catalog snapshot 转成可执行的 harness manifest。adapter 会优先匹配 MineDojo 官方 `tasks_specs.yaml` 模板，从 task id 反推出模板变量，然后生成：

- `verifier`：Harvest 使用 inventory delta，Combat 使用 kill-entity stat delta，TechTree 使用 use-item stat delta，Survival 使用 alive-time delta。
- `reset_plan`：清背包、清掉落物、设置时间/天气、设置初始装备、生成初始 mob、保留 biome hint。
- `minedojo.source_spec`：记录是否来自官方模板、模板 id、模板变量和置信度。

生成全量 executable manifest dry-run：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/build_minedojo_executable_manifests.py \
  --pretty
```

生成 JSONL 快照供 `MineDojoTaskProvider` 直接读取：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/build_minedojo_executable_manifests.py \
  --output-jsonl runs/minedojo_executable_manifests.jsonl \
  --summary-path runs/minedojo_executable_manifests.summary.json \
  --pretty
```

之后可以把 live runner 的 `--manifest-dir` 指向这个 JSONL：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --manifest-dir runs/minedojo_executable_manifests.jsonl \
  --task-id harvest_1_log \
  --rcon-reset \
  --rcon-port 25575 \
  --rcon-password <PASSWORD> \
  --auto-promote \
  --max-steps-per-task 12
```

注意：Harvest 类任务默认不会凭空生成目标方块。它只按 MineDojo 语义设置初始条件，让 agent 在当前世界中寻找并采集目标。Combat 类任务会根据 `initial_mobs` 通过 RCON 在 bot 附近生成目标实体。

## MineDojo-style RCON Reset

`ServerCommandResetRuntime` 现在会读取 manifest 中的 `reset_plan`，并生成可审计的 Minecraft 1.20.1 命令：

- `/clear <bot>`：清空或清理目标物品。
- `/kill @e[type=item]`：清理掉落物，避免上个 task 污染 verifier。
- `/time set ...`、`/weather ...`：设置时间天气。
- `/item replace entity <bot> <slot> with minecraft:<item> <count>`：设置初始装备。
- `/execute at <bot> run summon minecraft:<entity> ~x ~y ~z`：生成初始 mob。
- `/execute at <bot> run setblock ~x ~y ~z minecraft:<block>`：为需要的 reset setup 放置方块。

每次 reset 的 command plan、单条命令响应、耗时和错误都会出现在 `environment_reset` / `run_started.reset_result` 审计 payload 中。

## Skill 语义更新

Week10C 开始，skill 默认不是确定性 macro，也不会把 `execute_skill` 暴露给模型。skill 是上下文中的程序化经验，模型读取后仍然通过当前 observation 自主选择原子动作。

新的 skill 字段：

- `strategy_summary`：自然语言策略总结，强调不能盲目复用 source 坐标。
- `parameterized_plan`：参数化步骤，例如“从 scan result 选择目标方块”“移动到可达拾取范围”。
- `recovery_policy`：不可达、掉落物未拾取、目标实体不可见等恢复策略。
- `source_evidence`：source run、source steps、source action types，供审计/replay。
- `verifier_stats`：source task verifier 和 run status。

`action_plan` 继续保留，但只用于审计和 replay，不作为默认执行工作流。

## 多端口并行与硬件估算

新增 `runtime.server_pool` 中的 server-pool 配置和资源估算。正式并行模式现在强制一个 worker 绑定一个 server、一个 RCON endpoint 和一个 world 目录；单 server 多 worker 只允许显式传入开发选项 `--allow-shared-server-workers`。训练报告会记录完整 placement 和保守资源估算。对于本机 Apple M5、10 核、32GB 内存，推荐默认：

- 稳定开发：1 个 server + 1 个 worker。
- 隔离训练：2 个 server 实例，每个 1 个 bot，每个 Java heap 约 2.5-3GB。
- 保守上限：3 个 server；不建议默认 4 个以上。

本机 2026-07-12 双服 smoke 实测每个 Java 进程 RSS 约 2.8GB，总 RSS 约 5.8GB；加上 worker、PostgreSQL、Redis、前端和系统余量后，保守总预算为 9.7GB。默认不启动 5 个本地 server。

当前已经提供多实例启动入口：

```bash
MINECRAFT_RCON_PASSWORD=<PASSWORD> \
PYTHONPATH=backend/src backend/.venv/bin/python scripts/start_minecraft_server_pool.py \
  --server-count 2 \
  --first-server-port 25565 \
  --first-rcon-port 25575 \
  --heap-gb 2.5
```

关闭多实例：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/stop_minecraft_server_pool.py
```

启动器会等待 game/RCON 端口就绪，在 `infra/minecraft-server-pool/server-1`、`server-2` 下生成隔离 server 目录，并写入 `server_pool_state.json`。live runner 会真实读取该文件完成 worker placement，不再只把它当作展示信息。

也可以使用：

```bash
make minecraft-pool-up
make minecraft-pool-down
```

## MineDojo 数据来源

导入脚本使用这些官方文件：

- `programmatic_tasks.yaml`：1581 个 programmatic tasks 的官方 prompt、guidance、category。
- `tasks_specs.yaml`：MineDojo programmatic task specification 源文件。
- `tasks_suite.yaml`：官方 standard/difficult benchmark subset 标签。

本地输出：

- `tasks/catalog/minedojo_programmatic_tasks.jsonl`
- `tasks/catalog/minedojo_programmatic_tasks.summary.json`

当前类别统计：

- Combat：471
- Harvest：895
- Survival：2
- TechTree：213

## 如何导入

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/import_minedojo_programmatic_catalog.py
```

预期 summary：

```json
{
  "task_count": 1581,
  "categories": {
    "combat": 471,
    "harvest": 895,
    "survival": 2,
    "techtree": 213
  }
}
```

脚本也支持通过 `--programmatic-file` 和 `--tasks-suite-file` 读取本地 YAML，因此 CI 不需要联网。

## 差异化 Batch Planning

任务相似度来自：

- category/family
- goal text
- knowledge tags
- allowed action set
- verifier target 或 MineDojo task target tokens

Planner 使用 greedy 策略，每一步选择与已选任务最大相似度最低的任务。它服务的是并行 single-agent skill training：

```text
worker1 -> task A -> candidate skill
worker2 -> task B -> candidate skill
worker3 -> task C -> candidate skill
```

这些 agent 不互相通信。做 diversity 的目的，是降低同一个 epoch 中重复 skill candidate 和资源冲突的概率。

从完整 catalog 规划一个 batch：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/plan_week10_diverse_batch.py \
  --batch-size 10 \
  --max-task-similarity 0.45
```

从 curated manifests 中选一个可执行 diverse smoke batch 并运行：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --diverse-batch-size 5 \
  --worker-concurrency 5 \
  --output-dir runs/week10-smoke
```

## Live 并行训练

`LiveTrainingRunner` 是 Week 10B 的真实 Minecraft 训练路径。它仍然是 parallel single-agent training，不是 multi-agent：

```text
worker-1 -> bot username A -> task A -> verifier -> skill candidate
worker-2 -> bot username B -> task B -> verifier -> skill candidate
```

workers 之间不通信。它们只共享 SQL skill library，写入路径统一经过 candidate creation、duplicate detection 和可选 promotion。

主要文件：

- `backend/src/mc_agent_harness/training/live_runner.py`
- `scripts/run_week10_live_training.py`

连接 Minecraft LAN server 的 scripted live smoke：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT> \
  --task-id minedojo_harvest_oak_log \
  --worker-concurrency 1 \
  --scripted \
  --auto-promote \
  --start-delay-sec 30 \
  --max-steps-per-task 5
```

两个 worker 的隔离并行 scripted live smoke：

```bash
export MINECRAFT_RCON_PASSWORD=<PASSWORD>

PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --server-pool-state infra/minecraft-server-pool/server_pool_state.json \
  --manifest-dir tasks/executable/minedojo_programmatic_tasks.jsonl \
  --task-id combat_chicken_forest_barehand \
  --task-id harvest_1_dirt \
  --worker-concurrency 2 \
  --scripted \
  --rcon-reset \
  --rcon-random-teleport-when-biome-missing \
  --clear-all-inventory-on-reset \
  --max-steps-per-task 1 \
  --max-runtime-sec-per-task 90
```

`biome_hint` 存在时，reset 会审计 `/locate biome` 和 `/spreadplayers`，并在同一 server 内缓存 biome 坐标；没有 `biome_hint` 时，上述 fallback 只做随机出生。显式 `start_position` 或全局随机出生配置仍具有更高优先级。

## 100 个任务正式试跑

先生成不连接 Minecraft、不调用 LLM 的可复现计划：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_formal_batch.py \
  --dry-run \
  --task-count 100 \
  --worker-concurrency 2 \
  --max-task-retries 5
```

当前全量 executable snapshot 会按数据集比例得到 `57 harvest / 30 combat / 13 techtree`。调度器再生成低相似度 wave；阈值内找不到第二个任务时保留单任务 wave，不为了填满两个 worker 强行并行近重复任务。

正式运行：

```bash
export MINECRAFT_RCON_PASSWORD=<PASSWORD>
make docker-up
make minecraft-pool-up
make week10-formal-100
```

`--max-task-retries 5` 表示每个任务执行一次初始 attempt，失败后最多再执行 5 次，总上限为 6 次；成功后立即停止该任务的后续重试。每次 attempt 都写入 `attempt_outcomes`，`outcomes` 只保存每个 task 的最终结果。正式双 worker LLM run 默认使用 PostgreSQL；SQLite 只允许单 worker 或显式开发 smoke。

每个 wave 完成后会原子写入 `week10_formal_batch.checkpoint.json`。中断后使用原来的输出目录恢复：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_formal_batch.py \
  --resume \
  --output-dir runs/formal/<原目录>
```

恢复会校验 task ids、wave plan 和未完成阶段的 skill/learning snapshot revision。checkpoint 只记录完整 wave，正在执行的半个 wave 会安全重跑。skill candidate 以 `source_run_id` 幂等创建，避免 finalize 恢复生成第二个版本。

skill snapshot 在整个 100-task batch 开始时冻结。所有 wave 和 retry 完成后，才统一执行 failure classification、recovery validation、dedup 和可选 promotion；同批任务不能读取彼此刚生成的 skill。

报告中的每个 attempt 以及 batch 总览都包含 `model_call_count/input_tokens/output_tokens/total_tokens`。malformed JSON、非法 action 和 repair 失败调用也进入 `model_calls`，避免成本统计只计算最终合法 action。

人工布置资源时，`minedojo_harvest_dirt` 可以使用自然地面或手动放置的 dirt。worker 会按附近 `dirt` 目标自然挖掘，并在挖出一格坑后尝试水平移动到掉落物上方完成拾取。

LLM 版本验证：

scripted smoke 只验证 worker 并行和训练链路，不验证模型。验证真实 LLM 时，先跑一次不连接 Minecraft 的模型版本检查：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py
```

输出会写入 `runs/llm_verify_<timestamp>.json`，并在终端打印：

- `request_model`：本次请求使用的 `MODEL_DEFAULT` 或 `--model`。
- `response_model`：模型服务端返回的实际模型字段；如果 provider 不返回该字段则为空。
- `provider`：当前 `ModelProfile.provider`。
- `usage`：provider 返回的 token 用量。
- `action`：模型输出并通过 harness schema 校验后的动作。

如果需要临时验证另一个模型 id，不改 `.env` 也可以：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py \
  --model qwen3.7-plus
```

真实 LLM live training 去掉 `--scripted`：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT> \
  --diverse-batch-size 2 \
  --worker-concurrency 2 \
  --auto-promote \
  --start-delay-sec 30 \
  --max-steps-per-task 8
```

## Prompt 与 Harvest 原子动作

Week 10 追加了 prompt/action contract 调整，用来移除早期过粗的 worker 宏动作。目标是让采集任务的 procedure 进入 skill library，而不是被 worker 层宏动作吞掉。

当前 prompt 分为三层，知识检索默认由 agent 主动调用只读工具：

- 静态 system prompt：agent 角色、行为准则、禁止 raw Mineflayer/MineDojo code、禁止编造状态、探索策略和 skill 使用原则。
- 稳定 harness contract：allowed action primitives、knowledge tool contract、runtime hints 和机器可读 decision envelope；同一 action profile 下逐轮不变。
- 动态 user payload：当前 task、state summary、compact evidence、task memory、skills、learning candidates 和 run context。

静态 prompt 和稳定 contract 合并为一个 `role=system` message，逐轮变化的数据只进入 `role=user`，让 Qwen/OpenAI 风格服务能复用稳定 prefix cache。每一步完整 prompt 仍写入 `context_built` 审计事件：

```sql
select payload
from trajectory_events
where event_type = 'context_built'
order by id
limit 1;
```

也可以不跑 Minecraft，直接 dump 某个任务首步 prompt：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/dump_agent_prompt.py \
  --task-id minedojo_harvest_oak_log \
  --pretty
```

Harvest curated manifests 当前默认暴露这些原子动作：

- `scan_blocks`：扫描附近目标方块，不改变世界。
- `move_to`：移动到某个坐标附近。
- `dig_block_at`：挖指定坐标的方块。
- `scan_dropped_items`：扫描附近掉落物，不移动。
- `wait_ticks`：等待原版拾取和短暂世界状态更新。
- `query_inventory`：确认背包状态。
- `request_visual_snapshot`：文本观察不足时请求视觉帧。

旧采集宏动作已从 schema、worker dispatch、prompt contract、测试和 smoke 脚本中移除。`minedojo_harvest_oak_log`、`minedojo_harvest_dirt`、`minedojo_harvest_sand`、`minedojo_harvest_cobblestone` 的 scripted trace 已改为原子序列。成功轨迹预期沉淀为类似：

```text
scan_blocks(target) -> move_to(block_position) -> dig_block_at(block_position) -> scan_dropped_items(drop) -> move_to(drop_position) -> wait_ticks -> query_inventory
```

注意：当前 skill candidate 的 `action_plan` 保留 source trajectory 的具体 action args，用于可回放审计；同时 `validation.parameterized_plan` 会记录泛化后的 review plan，例如“从 scan result 中选择目标”“使用 nearest_reachable_position 恢复不可达路径”。后续真正执行 skill 时需要由 skill executor 把参数化 plan 绑定到当前 observation。

live training 会启用 inventory delta verifier：`inventory_contains` 不再只看最终背包是否有目标物品，而是要求本次 run 从第一帧 observation 到最终状态净增目标物品。因此测试前应清空 bot 背包；如果 bot 第一帧已经有 `oak_log` 或 `dirt`，不会被判定为成功，也不会沉淀 skill。

skill candidate 现在由 `SkillCreationPolicy` 和 `SkillSummarizer` 的初版管线创建：verified success 只是必要条件，非平凡 workflow、失败恢复、跨任务族复用、重复度低或显著节省成本才会进入 candidate。候选 `action_plan` 从成功的 progress action 中提取，例如 `scan_blocks`、`scan_dropped_items`、`move_to`、`dig_block_at`、`wait_ticks`、`craft_item`、`place_block`、`fight_entity`、`use_item`、`execute_skill`。纯 `query_inventory`、纯 `request_visual_snapshot` 或简单 recipe 且知识库已有覆盖的轨迹，默认 skipped。当前 summarizer 是 deterministic 版本，后续可以替换为 LLM 总结策略、触发时机和失败恢复边界。

## Agent-driven Knowledge Retrieval

知识库不再由 ContextManager 默认替模型检索并拼接。当前实现已经把知识库暴露为只读 harness tools：

- `resolve_terms(text)`：解析 canonical IDs、别名和类型。
- `get_recipe(item_id)`：返回配方、输入材料、产出数量和工作站需求。
- `retrieve_docs(query, limit, scope)`：从本地 Minecraft wiki 摘要、Mineflayer 操作指南或项目知识库中检索短片段。

模型负责判断是否需要检索、检索什么；harness 负责安全边界：

- 默认只允许离线知识库，Web Search 默认关闭。
- 开启线上 wiki 时必须使用 domain allowlist、长度预算、HTML/script 清洗、prompt-injection 降权和来源标注。
- 每次知识工具调用写入 `trajectory_events` 的 `knowledge_tool_call`，包括 query、tool args、source ids、返回摘要和截断预算。
- 相同 `action type + args` 的确定性查询使用 run 内 exact cache；cache hit 也生成审计事件，不会再次访问 provider。
- 知识工具结果先作为下一轮 ReAct observation 返回，之后保存在低优先级 `run_context.knowledge` 中。
- 知识不进入长期 trajectory；压缩时会先从完整结果降为摘要，再被驱逐。被驱逐后模型可以重新查询。

## Run 内上下文分层压缩

原始 observation、decision、action result 始终完整保存在数据库，压缩只影响发送给模型的 prompt：

1. 最新一步由 `compact_evidence.previous_step` 保留 action-specific 证据。
2. 较近步骤保留压缩后的动作结果，例如导航距离变化、背包增量、掉落证据和战斗 kill evidence。
3. 更早步骤按 `navigation`、`collection`、`processing`、`combat`、`interaction` 语义阶段合并。
4. 超过预算时进入 `aggressive`，再不足则进入 `episode`，只保留 action counts、progress signals 和最近失败。

知识查询与世界轨迹分开管理。上一动作是知识查询时，其结果只出现在 `previous_step`，不会同时在知识账本重复；下一世界动作后才由账本提供。`RunContextMemory` 随 checkpoint 保存，resume 后仍可继续压缩和命中知识 cache。默认 run context 预算为 12000 字符，其中知识最多 3500 字符且优先驱逐。

## `dig_block_at` 掉落证据

Minecraft vanilla server 没有通过 RCON 提供“某次挖掘直接产出了哪个 item”的订阅接口。worker 因此使用 Mineflayer 收到的服务器权威数据做有边界归因：动作前记录背包和现有掉落实体，方块被挖后等待一个短窗口，再比较背包增量，并读取该方块附近新出现的 item entity。

- `inventory_gained`：挖掘窗口内服务器确认物品进入背包，具体物品在 `inventory_delta`。
- `drop_entity_observed`：服务器确认方块附近出现新的掉落实体，具体物品和坐标在 `spawned_drops`。
- `no_drop_observed`：观察窗口内没有掉落证据；不会根据方块类型猜测已掉落。
- `dig_incomplete_no_drop_claim`：挖掘超时，明确不作掉落声明。

结果同时保留 `block_removed`，避免把“方块消失”和“已经获得掉落物”混为一谈。`drop_evidence_source=minecraft_server_entity_packets_and_inventory` 说明证据来自服务端同步的实体/背包状态，而不是 LLM 推断。

输出：

- `runs/week10_live_training_<timestamp>.json`
- `runs/week10_live_training_<timestamp>.sqlite3`

SQLite DB 会包含 `runs`、`steps`、`trajectory_events`、`model_calls`、`runtime_errors`、`task_memories` 和 `skills`。如果 verifier 失败，runner 会在当前 worker 的 memory namespace 下写一条 task-local memory。如果 verifier 成功，runner 会先进入 skill creation policy 和 summarizer，再调用 `SkillLibrary.find_duplicates(...)` 做重复检测；只有通过 replay/去重/质量门控后才会 promotion。

## Skill Candidate 去重

`SkillCandidateDeduper` 会用以下特征比较 candidate skill 和已有 draft/validated/staged/promoted skills：

- action types
- action targets
- triggers
- task scope
- dependencies
- name tokens

`SkillLibrary.find_duplicates(candidate, threshold=0.82)` 会返回近似重复项。它目前不会自动阻止 promotion；后续 promotion coordinator 再决定 merge、降级还是进入人工 review。这样 Week 10 不会破坏 Week 7 已有生命周期语义。

## 验证方式

推荐优先使用自动化脚本。默认不连接 Minecraft，会执行 worker typecheck、Week10 核心单测、JSON schema 校验、prompt dump 和 deterministic benchmark，并把结果完整保存到一个目录：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py
```

默认输出目录类似：

```text
runs/week10_automated_<timestamp>/
```

目录内容：

- `summary.json`：机器可读总结果。
- `summary.md`：人类可读测试摘要。
- `metadata.json`：测试参数、git status、时间戳。
- `logs/`：每个命令的 stdout/stderr。
- `prompts/`：完整 prompt dump。
- `benchmark/`：deterministic benchmark JSON/Markdown 报告。

如果要固定输出目录：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py \
  --output-dir runs/week10_manual_check
```

如果 Minecraft LAN 已开启，并且要加入 live scripted 测试：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py \
  --live-port <LAN_PORT> \
  --live-scripted \
  --auto-promote \
  --clear-inventory-on-reset \
  --start-delay-sec 30
```

如果要测试真实 LLM：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py \
  --live-port <LAN_PORT> \
  --live-llm \
  --auto-promote \
  --clear-inventory-on-reset \
  --start-delay-sec 30
```

`--live-llm` 会先执行 `verify_llm_model.py`，再执行真实 LLM live training。live 结果会额外写入：

- `live_scripted/week10_live_training.json`
- `live_scripted/week10_live_training.sqlite3`
- `live_llm/week10_live_training.json`
- `live_llm/week10_live_training.sqlite3`

### Live Reset 清背包

为配合 inventory delta verifier，live runner 支持在 worker reset 阶段清理 bot 背包。最常用方式：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT> \
  --task-id minedojo_harvest_oak_log \
  --clear-inventory-on-reset \
  --auto-promote \
  --start-delay-sec 30 \
  --max-steps-per-task 8
```

如果只传 `--clear-inventory-on-reset`，runner 会从 verifier 中推断目标物品，例如 `oak_log`，并在 `runtime.reset_policy` 中写入：

```json
{"clear_inventory": {"enabled": true, "mode": "items", "items": ["oak_log"], "drop_fallback": true}}
```

也可以显式指定清理物品：

```bash
--clear-inventory-on-reset --clear-item oak_log --clear-item dirt
```

或清空全部背包：

```bash
--clear-all-inventory-on-reset
```

worker 会先尝试 Minecraft `/clear` 命令。这个路径最接近 MineDojo 的 reset 语义，但 MineDojo 是通过 Malmo/Minecraft 服务端桥接执行命令；当前 Mineflayer worker 如果连接的是普通 LAN 世界，本质上是一个加入世界的玩家，可能没有命令权限。命令失败时，worker 会记录服务器反馈，并默认启用 `drop_fallback`：通过 Mineflayer 普通玩家 API 把匹配物品或全背包 `tossStack` 掉，避免 agent 因为旧背包里已有目标物品而停止行动。

`drop_fallback` 不需要 OP 权限，但它只是把物品丢到世界里，不能像服务端 `/clear` 一样删除掉落物。真实训练推荐使用以下任一方式提供服务端 reset 权限：

- LAN 开启允许作弊，并给 bot 用户名 OP/命令权限。
- 使用独立 Minecraft server，在 `ops.json` 中加入训练 bot 用户。
- 后续接入 MineDojo/Malmo 风格的服务端命令通道或 reset mod/datapack，由服务端在 join/reset 时清背包、清掉落物、设置时间/天气/biome。

如需禁用丢弃兜底，传入：

```bash
--no-reset-drop-fallback
```

### RCON 服务端 Reset

为了更接近 MineDojo/Malmo 的 reset 权限模型，live runner 支持通过 Minecraft RCON 执行 harness-owned 服务端命令。RCON 命令不会进入 prompt，也不是 LLM action；它只属于环境 reset 层，并写入 `environment_reset.server_command_reset` 审计结果。

Minecraft server 需要开启 RCON：

```properties
enable-rcon=true
rcon.port=25575
rcon.password=<your_password>
```

本地运行时可用环境变量保存密码，避免写入任务 spec：

```bash
export MINECRAFT_RCON_PASSWORD=<your_password>
export MINECRAFT_RCON_HOST=localhost
export MINECRAFT_RCON_PORT=25575
```

单任务 live LLM 测试命令：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --task-id minedojo_harvest_oak_log \
  --worker-concurrency 1 \
  --max-steps-per-task 20 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-set-time day \
  --rcon-set-weather clear \
  --auto-promote
```

并行 smoke：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --task-id minedojo_harvest_oak_log \
  --task-id minedojo_harvest_dirt \
  --worker-concurrency 2 \
  --max-steps-per-task 20 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-set-time day \
  --rcon-set-weather clear \
  --auto-promote
```

reset 时随机传送：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --task-id harvest_1_feather \
  --worker-concurrency 1 \
  --max-steps-per-task 40 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-set-time day \
  --rcon-set-weather clear \
  --rcon-random-teleport-on-reset \
  --rcon-random-teleport-center-x 0 \
  --rcon-random-teleport-center-z 0 \
  --rcon-random-teleport-max-range 500
```

`--rcon-random-teleport-on-reset` 会在 reset plan 中注入 `random_teleport`，并通过 RCON 调用 Minecraft `/spreadplayers`。默认会移除 manifest 里的 `start_position`，避免后续 `/tp` 把随机传送覆盖掉。只有你明确想保留两个命令时，才使用 `--rcon-random-teleport-keep-start-position`。这个功能可以提升任务隔离和初始观察多样性，但不保证目标 biome 或目标资源一定在附近。

默认 RCON reset 会根据 worker username 和 reset policy 生成：

```text
/clear <worker_username>
/kill @e[type=item]
/kill @e[tag=mc_agent_owner_<normalized_worker_username>]
```

如果 reset policy 是 target-item mode，则生成：

```text
/clear <worker_username> minecraft:<target_item>
/kill @e[type=item]
/kill @e[tag=mc_agent_owner_<normalized_worker_username>]
```

通过 reset plan 生成的 mob 会同时带有 `mc_agent_task_mob`、worker owner tag 和由 `task_id` 派生的 task tag。下一次 reset 只清理同一 worker 上个任务生成的实体，不会删除自然生成的 mob，也不会干扰同一 server 中其他并行 worker。task tag 用于追溯实体来源；清理命令和带 tag 的 summon 命令都会写入 `environment_reset` 审计事件。

RCON 解决的是服务端 reset 自动化，不等于已经完成全量 MineDojo programmatic task 的执行映射。Week10 最终链路仍需要把 catalog task 转成可执行 harness manifest：目标物品/实体/方块、allowed actions、verifier、必要初始物品、biome/world setup、spawn mobs/setblock 策略。当前 RCON 是该链路的 reset 权限基础。

审计位置：

```sql
select payload
from trajectory_events
where event_type = 'environment_reset'
order by id desc
limit 1;
```

聚焦测试：

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/unit/test_week10_catalog_and_similarity.py \
  backend/tests/unit/test_week10_live_training.py \
  backend/tests/unit/test_skill_library.py
```

Prompt/action contract 快速验证：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/dump_agent_prompt.py \
  --task-id minedojo_harvest_oak_log \
  --pretty
```

完整验证：

```bash
make ci
```

## Agent 主视角录屏

live training 脚本现在可以自动录制 agent 任务行为视频，不需要手动打开 QuickTime/OBS。当前实现是客户端旁路录屏：你保持一个 Minecraft 客户端连接到同一个 server，脚本通过 RCON 把这个客户端切到 spectator 模式并跟随第一个 bot，同时自动启动/停止 `ffmpeg`。本次录屏的路径、ffmpeg 命令、spectate 命令结果会写入 live run JSON 的 `recording` 字段。

示例：

```bash
export MINECRAFT_RCON_PASSWORD=<your_password>

PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <SERVER_PORT> \
  --task-id harvest_1_glass_plains_with_furnace_and_fuel \
  --worker-concurrency 1 \
  --max-steps-per-task 30 \
  --max-runtime-sec-per-task 600 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-port <RCON_PORT> \
  --record-agent-video \
  --recording-input "4:none" \
  --recording-window-title Minecraft \
  --spectator-player <YOUR_CLIENT_PLAYER_NAME> \
  --recording-output runs/demo/agent_pov.mp4 \
  --auto-promote
```

`--spectator-player` 现在与录屏解耦：可以只传该参数，通过 RCON 把客户端切到旁观者并跟随第一个 bot，而不启动 `ffmpeg`。如果不传 `--spectator-player`，录屏只捕获当前可见屏幕，不会自动移动客户端视角。macOS 默认使用 `Capture screen 0:none` 作为 `ffmpeg` 的 avfoundation 输入；如果设备名不可用，可以查看设备编号并显式传入屏幕设备，例如 `--recording-input "4:none"`：

旁观者绑定现在会等待已经持久化的 `run_started` 事件，该事件只会在 reset 完成后发出。每个 run 开始时，harness 会先强制解除旧相机绑定、重新进入 spectator 模式、把客户端传送到 bot 身边，等待客户端同步区块和实体后再执行 `/spectate`。这样可避免 bot 被 reset 随机传送很远后，服务端认为相机已绑定、客户端却仍渲染旧位置。可通过 `--spectator-chunk-sync-delay-sec` 调整同步等待时间（默认 `0.75` 秒），通过 `--spectator-rebind-interval-sec` 调整保活重绑间隔（默认 `10` 秒）。每次命令结果都会实时持久化为 `spectator_follow_attempt`；中断的 run 会终结为 `cancelled`，不会残留在 `running`。

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

`--recording-window-title` 会通过 AppleScript 查找可见窗口，并自动生成 crop filter。如果 macOS 没有暴露 Minecraft Java 窗口，可以先把客户端移动到当前桌面、退出全屏模式，或者直接传手动裁剪参数：

```bash
--recording-filter "crop=<width>:<height>:<x>:<y>"
```

## 有边界实时战斗动作

Week10 的 combat task 需要实时反应，但不能把整场战斗完全交给模型逐 tick 调用。当前设计把战斗拆成三个模型可见原子动作：

- `scan_entities`：扫描目标实体，返回距离、高度差、视线、是否可能飞行、近战是否可达、建议模式。
- `move_to_and_engage_combat`：worker 自动接近并动态跟踪选定实体，在有时间上限的交战中使用当前装备攻击；模型仍负责选择 `melee/ranged`、装备和是否继续。动作会在击杀、低血量、不可达、无视线、无弹药或超时时返回控制权。
- `consume_item`：模型在战斗返回 `low_health` 或长任务状态变差时主动选择补给。

`engage_combat` 和 `fight_entity` 只保留为旧 manifest/trajectory 的兼容别名，不再拼接到 prompt 中。新动作不会自动装备或自动回血；串行 freeze 仍由 observation threat-pause 控制，与动作内动态追踪职责分离。skill 沉淀也不把战斗轨迹作为固定宏执行，而是总结成策略型经验。

当 task verifier 明确指定 entity 目标时，skill candidate 会先过滤不匹配的 `scan_entities`、`move_to_and_engage_combat`、旧 `engage_combat` 和 `fight_entity` 步骤。例如 chicken task 中用于自卫的 slime 战斗不会进入 `defeat_chicken` 的参数化 plan。被过滤步骤仍保存在 `source_evidence.excluded_source_steps` 中供审计。item verifier 不启用该过滤，因此“采集 feather 前击杀 chicken”这类必要前置步骤不会被误删。

## 失败经验分层沉淀

Week10 不再使用“失败次数达到阈值就直接写 skill”的规则。该规则会把网络超时、随机出生点、偶发寻路失败和错误猜测永久写入 skill 库。当前实现参考 Hermes 的后台 skill review 思路，同时增加 Minecraft task verifier 所提供的强验证门槛，把数据分成三层：

1. 原始失败轨迹：保留完整 observation、model decision、action、result、knowledge call 和 verifier，不直接进入模型长期上下文。
2. `LearningCandidate`：只保存归一化后的待验证假设，包括 task scope、失败动作、失败状态、知识来源、支持 run 和恢复 run。
3. `SkillSpec`：只有成功恢复并通过 task verifier 的候选，才允许补强或触发策略型 skill candidate。

状态机为：

```text
observed -> hypothesized -> corroborated -> validated -> promoted
                    \-> rejected / expired
```

- `observed`：第一次出现可复用的 gameplay 失败，但没有知识证据。该状态不会注入 prompt。
- `hypothesized`：agent 在该 run 中主动调用知识工具并获得相关来源；它仍是待验证假设。
- `corroborated`：相同 `scope + action + failure_status` 在独立 run 中重复出现至少两次。
- `validated`：后续同作用域 run 使用与失败动作匹配的恢复动作族，并最终通过 task verifier。
- `promoted`：validated candidate 已被写入并随 skill 一起晋升。

当前 durable failure allowlist 包括 `no_path`、`path_timeout`、`target_unreachable`、`target_lost`、`no_line_of_sight`、`no_ammo`、缺少材料/工作站等可以改变策略的 gameplay 结果。`task_timeout` 本身不是学习证据；但若该 run 内包含至少两次同一静态目标附近的 `timeout_no_progress`，且每次持续至少 8 秒、距离变化不超过 0.5 格并带 pathfinder 诊断，则可以形成导航候选。目标坐标相差超过 2.5 格、移动实体或只有一次停滞时不沉淀。

以下情况只保留审计，不生成 candidate：

- `model_timeout`、worker/runtime/reset error，以及没有上述充分导航证据的 `task_timeout`；
- 单次 `target_not_found`，因为目标可能只是没有随机刷新在附近；
- verifier failed 但轨迹里没有可归因的 durable action failure；
- 一次性环境坐标或未经验证的模型猜测。

世界事实和程序经验分开处理。例如“Enderman 会瞬移”应来自带来源的 knowledge document；skill 中可沉淀的是“`target_lost` 后重新扫描并重新锁定，不追逐旧坐标”。knowledge call 只作为引用存入 candidate，不能单独把模型猜测提升为事实。

并行训练继续使用 batch barrier：batch 开始时同时冻结 promoted skill snapshot 和 active learning-candidate snapshot；worker 运行期间不能看见同批次新写入的经验。全部任务结束后依次执行：

1. 对最终失败 run 分类、去噪并按 signature upsert；
2. 用成功 run 配对同作用域候选并验证恢复模式；
3. 将 validated learning evidence 交给 `SkillLibrary`，再执行 candidate policy、dedup 和 promotion。

数量变体不会拆分候选。`harvest 1/2/8 oak_log` 都映射到 `harvest:oak_log` scope；signature 由 `scope_key + action_type + failure_status` 构成。SQL 层对 signature 做唯一约束，更新时使用行锁，避免并行 finalize 生成重复记录。

下一批 prompt 只检索完全相同 scope 的 `hypothesized/corroborated/validated` candidate，并附带：

```json
{"semantics":"scoped_hypothesis_not_authoritative_instruction"}
```

模型可以根据当前 observation 决定是否采用或继续查询知识，harness 不会把失败假设当作强制步骤。`observed` 不进入上下文，`promoted` 由正式 skill retrieval 接管。

审计事件包括：

- `learning_candidate_skipped`
- `learning_candidate_created`
- `learning_candidate_updated`
- `learning_candidate_validated`
- `learning_candidates_promoted`

候选可以通过 `/api/learning-candidates` 和 `/api/learning-candidates/{id}` 查看；run replay 的 `context_built.retrieved_learning_candidates` 可确认每一步实际向模型暴露了哪些假设。数据库查询示例：

```sql
select signature, scope_key, status, support_count, recovery_count, confidence
from learning_candidates
order by updated_at desc;
```

当前 skill 文案由受约束的确定性 summarizer 从成功轨迹、knowledge references 和 verifier-backed recovery 生成，原始 action trajectory 只保留作审计/replay，不作为默认宏执行。后续可以增加 LLM reviewer 草拟更自然的 `strategy_summary`，但 LLM 输出仍必须通过当前状态机、source evidence、dedup 和 verifier 门槛，不能自行 promotion。

## 当前限制

- 完整 catalog 任务目前是 catalog-only，不能全部直接跑 deterministic benchmark runner。
- Live training 当前执行的是可运行的 harness manifests，不是任意 catalog-only records。
- 本地 catalog 还没有把 MineDojo 展开后的低层 simulator specs 全量写入每条记录。
- 相似度是确定性的 lexical/set similarity，还不是 embedding 语义相似度。
- dedup 会发现重复 skill candidate，并阻止近似重复 candidate 自动 promotion；merge policy 还没有自动化。
- failure learning 当前只提取每个 run 中最后一个 durable failure，暂不做多段轨迹的因果归因。
- LLM reviewer 尚未参与最终 skill 文案生成；当前优先保证沉淀证据可复现、可测试。
