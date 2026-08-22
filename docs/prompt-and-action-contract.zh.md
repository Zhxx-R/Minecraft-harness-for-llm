# Agent Prompt 与动作原语说明

## 当前静态 System Prompt 中文版

> 说明：运行时代码里仍使用英文 prompt；下面是当前英文 prompt 的中文对照版，用于设计评审、面试讲解和后续调参。

```text
你是一个运行在 harness 中的 Minecraft 任务求解 agent。

角色：
你通过每次选择一个经过审计的 harness action 来解决 Minecraft 任务。

行为规则：
只能使用当前 action contract 中列出的、经过校验的 harness actions。
不要编写原始 Mineflayer JavaScript、MineDojo Python、shell 命令或自由形式代码。
不要编造方块、物品、实体、坐标、配方、背包内容或 skill 结果。
当目标不清楚时，优先选择广泛但低成本的探索：扫描、检查背包，或在允许时请求视觉快照。
需要 Minecraft 术语、配方、工具需求或 Mineflayer 操作语义时，主动调用知识库工具；不要假设隐藏上下文已经提供了全部知识。
当可恢复失败可能取决于未知的 Minecraft 机制时，先检索相关知识，再决定是否重复原动作。
只有当检索到的 promoted skill 的触发条件和前置条件与当前任务匹配时，才优先使用该 skill。
当具体证据表明任务目标已经满足时，如果 submit_for_evaluation 可用，调用它请求结束；是否成功由 evaluator 决定，不由你决定。
只返回一个符合如下形状的 JSON 对象：
{"reasoning_summary":"选择该动作的简短可审计理由，不是隐藏思维链","evidence":["使用的 observation、上一动作、skill 或知识证据"],"knowledge_need":{"needed":false,"query":null,"reason":null},"action":{"type":"query_inventory","args":{}}}
```

## 稳定 Harness Contract

`ContextManager` 把静态 prompt 和稳定的 harness contract 合并为一个 `role=system` 消息。该 contract 只由当前 action profile 决定，不包含 task、observation、memory 或历史动作，因此同一 run 的 system 前缀逐轮完全一致，便于模型服务命中 prefix cache。

```text
这是本次 run 的稳定 harness contract。请把它视为权威且可缓存的约束：
{
  "knowledge_tool_contract": resolve_terms、get_recipe、retrieve_docs 的只读边界,
  "available_action_primitives": 当前允许动作的参数说明、语义和限制,
  "runtime_hints": 与具体任务目标无关的运行时证据提示,
  "termination_contract": 模型请求结束、evaluator 验收和 harness 安全上限,
  "action_contract": allowed actions、结构化决策 envelope 和真实性规则
}
```

## 当前 User Payload 中文模板

实际发送给模型的 user message 是机器可读 JSON。中文等价结构如下：

```json
{
  "task": "当前任务规格",
  "state_summary": "给模型快速阅读的一句话或几句话状态摘要",
  "compact_evidence": {
    "goal_progress": "从 verifier 和当前 inventory 推导出的任务进度",
    "current_state": "压缩后的当前位置、血量、饥饿值、相关背包物品、相关附近方块和实体",
    "previous_step": "按上一轮 action 类型压缩后的 ReAct observation；第一轮为 null",
    "raw_evidence_available": true
  },
  "task_memory": "当前任务的局部记忆",
  "task_plan": null,
  "resolved_terms": "仅 auto-retrieval ablation 启用时存在",
  "retrieved_docs": "仅 auto-retrieval ablation 启用时存在",
  "retrieved_skills": "skill 检索结果",
  "retrieved_learning_candidates": "同 scope、尚待验证的失败假设",
  "run_context": {
    "trajectory": "旧动作的分层压缩轨迹",
    "knowledge": "可驱逐、允许重新查询的 run 内知识账本"
  }
}
```

## 当前 Prompt 分层

当前 `ContextManager` 会构造三类内容：

1. 静态 system prompt
   - 定义 agent 角色：Minecraft task-solving agent。
   - 定义行为准则：只能调用 harness action，不允许写 Mineflayer JS、MineDojo Python、shell 或自由代码。
   - 定义真实性约束：不能编造方块、物品、实体、坐标、recipe、inventory 或 skill 结果。
   - 定义探索策略：目标不清楚时先 scan、查 inventory 或请求视觉快照。
   - 定义 skill 策略：只有 retrieved skill 的 trigger 和 precondition 匹配时才优先使用。
   - 定义输出格式：只返回一个带简短 `reasoning_summary` 和 `evidence` 的 JSON decision envelope。

2. 稳定 harness contract
   - 可用 knowledge tools 的参数说明和安全边界。
   - 本次 run 的 allowed action primitives 及其参数说明。
   - 与任务类型无关的 runtime hints。
   - decision envelope、证据约束和 ReAct 规则。

3. 动态 user payload
   - 当前 task、task memory 和 retrieved promoted skills。
   - `state_summary` 与 `compact_evidence`。
   - 分层压缩后的 `run_context.trajectory`。
   - 低优先级、可重新查询的 `run_context.knowledge`。

静态 prompt 与稳定 contract 合并为一个 `role=system` message；所有逐轮变化的数据只进入 `role=user` message。这保证了 system 前缀不会因血量、坐标、任务历史或知识结果变化而失去缓存。

## 知识库工具边界

目标 contract 是 agent-driven knowledge retrieval：模型决定是否查知识库、查什么、查多少；harness 只负责把知识库能力作为受控工具暴露，并保证检索安全、可审计、可复现。

- `resolve_terms(text)`: 把任务文本或 observation 中的自然语言术语解析成 canonical Minecraft IDs、别名和类型。
- `get_recipe(item_id)`: 返回本地/离线知识库中的结构化配方、输入材料、产出数量和工作站需求。
- `retrieve_docs(query, limit, scope)`: 返回受控知识源中的短文档片段，例如 Minecraft wiki 摘要、Mineflayer 操作说明、项目内经验文档。

知识工具不是 runtime action，不会发给 Mineflayer worker。执行循环需要把模型输出分发到不同 tool domain：

- `runtime`: `move_to`、`dig_block_at`、`craft_item` 等会改变 Minecraft 状态的动作。
- `knowledge`: `resolve_terms`、`get_recipe`、`retrieve_docs` 等只读知识工具。
- `skill`: `search_skill`、`execute_skill`、`reflect_skill_candidate` 等 skill library 工具。

安全与合规由 harness 强制：

- 默认只允许本地离线知识库；Web Search 默认关闭。
- 若后续开启线上 wiki，只允许 allowlist domain，并记录 URL、时间、query、命中 chunk、截断长度和来源。
- 对检索结果做长度预算、HTML/script 清洗、prompt-injection 文本降权和来源标注。
- 每次知识工具调用写入 `trajectory_events`，包括 model query、tool args、source ids、返回摘要和 token/字符预算。
- 相同 `action type + args` 的确定性查询会命中 run 内 exact cache，并审计 `cache_hit`、`cache_signature` 和首次查询步骤。
- 知识结果先作为下一轮 `compact_evidence.previous_step` 返回；随后进入 `run_context.knowledge`，不会重复出现在同一轮两个位置。
- 知识是可重复获取输入，不进入耐久 trajectory。上下文压力增大时先从完整结果降为摘要，再整体驱逐；模型可以重新调用工具。

## 状态压缩边界

worker 仍然返回完整 JSON，recorder 仍然保存完整 `observation` 和 `action_result` 用于审计、回放、verifier 和 skill 提取。默认 prompt 不再直接塞入完整 worker JSON，而是由 `ContextManager` 调用 `state_summary.py` 生成两层输入：

- `state_summary`: 给模型读的自然语言状态摘要。
- `compact_evidence`: 给模型和后续工具使用的小型结构化证据。
- `raw_evidence_available`: 明确表示原始证据存在于审计日志，但当前 prompt 只展示压缩证据。

每个原子动作都有独立压缩策略：

- `scan_blocks`: 保留 query、scan radius、候选数量、最近目标坐标、距离和 `can_dig`。
- `scan_dropped_items`: 保留 query、候选掉落物、坐标、距离和数量。
- `move_to`: 保留目标坐标、tolerance、timeout、最终距离、当前位置和 timeout/recoverable 状态。
- `dig_block_at`: 保留目标坐标、期望方块、`block_before -> block_after`、手持工具、预计挖掘时间、`inventory_delta`、新生成掉落实体和掉落证据来源。
- `wait_ticks`: 保留等待 tick 数、`inventory_delta` 和 verifier 相关背包计数；移动到掉落物后依赖原版拾取机制，而不是额外收集宏动作。
- `query_inventory`: 只保留 verifier 相关物品计数；没有 verifier target 时才保留截断后的背包计数。
- `craft_item`: 保留目标 item、请求数量、craft count、产出数量、station 和 recipe/station 失败原因。
- `place_block`: 保留放置物品、目标位置、reference block 和 face。
- `use_item`: 保留使用物品、激活类型、目标 block/entity 和缺失目标原因。
- `move_to_and_engage_combat`: 保留目标、模式、动态跟踪进度、攻击/射击数、血量、不可达诊断和 server-confirmed kill evidence。
- `fight_entity` / `engage_combat`: 仅保留旧轨迹兼容，正常 prompt 隐藏。
- `request_visual_snapshot`: 保留截图是否可用、格式、引用或不可用原因。
- `execute_skill`: 保留 skill 名称、版本、子步骤数量、失败子步骤和 inventory delta。

旧动作上下文按层级压缩：最新动作始终由 `compact_evidence.previous_step` 提供；较近动作保留 action-specific compact evidence；更早动作按 `navigation`、`collection`、`processing`、`combat`、`interaction` 阶段合并；仍超预算时只保留 episode 级 action counts、progress signals 和最近失败。数据库中的原始 observation/action result 不受 prompt 压缩影响。

## Action 拼接边界

当前 prompt 拼接统一的 canonical primitive action set。任务 manifest 中的旧动作限制只作为历史元数据，不会缩小训练/测试时的模型动作空间。

- `allowed_actions`: 当前任务给模型开放的动作名。
- `available_action_primitives`: 上述动作的参数、返回值和使用语义。
- `task`: 进入 prompt 前会过滤 `runtime`、`training`、`start_delay_sec`、`manifest_allowed_actions`，并移除 benchmark 中的 `scripted_actions` 和 `initial_state`，避免历史动作限制、评测脚本或环境预设污染 agent 决策。

## Finish 与终止协议

当前 ReAct 循环把 `submit_for_evaluation` 定义为 harness control action，而不是 Mineflayer 原子动作。这与主流 tool-calling agent 的做法一致：模型发出结束意图，框架负责路由到终止状态，并保留超时、步数等独立安全条件。

```json
{
  "reasoning_summary": "Current inventory and verifier evidence indicate the requested target is present.",
  "evidence": ["inventory oak_log count is 1; target quantity is 1"],
  "knowledge_need": {"needed": false, "query": null, "reason": null},
  "action": {"type": "submit_for_evaluation", "args": {}}
}
```

协议边界如下：

- agent 只能“请求验收”，不能自行将 run 标记为 success。
- programmatic task 在当前 observation 上立即运行权威 verifier。通过后以 `agent_submitted_verified` 结束；提前提交会返回 `submission_rejected` 及 verifier 证据，下一轮继续 ReAct。
- creative task 的在线循环不伪造 MineCLIP 结果。提交被接受为 `agent_submitted_for_external_evaluation`，随后由独立 MineCLIP/人工阈值链路给出 `success`、`failure` 或 `inconclusive`。
- 没有 evaluator 的 run 仍允许 agent 主动结束，但只能记为 `agent_finished_unverified`：`task_success=null`、`evaluation_status=not_evaluated`，不得冒充成功。
- `max_steps`、`max_runtime`、runtime termination 仍是 harness 强制安全条件，不依赖模型主动结束。

审计事件包括 `agent_finish_requested`、`step_verifier_result`、`agent_finish_accepted` / `agent_finish_rejected` 和 `run_finished.stop_reason`。这能区分“模型认为已完成”、“verifier 确认完成”与“安全预算耗尽”。

设计参考：[ReAct](https://react-lm.github.io/)、[LangGraph `toolsCondition`](https://langchain-ai.github.io/langgraphjs/reference/functions/langgraph.prebuilt.toolsCondition.html) 和 [AutoGen termination conditions](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/termination.html)。本项目使用显式 control action，是因为模型输出已被限定为单个结构化 action envelope；结束意图因此也应走同一套 schema 校验和审计通道。

## 查看完整 Prompt

运行：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/dump_agent_prompt.py \
  --task-id minedojo_harvest_oak_log \
  --pretty
```

该脚本默认使用 live training 的完整 harness action scope，因此能看到知识工具和 `move_to_and_engage_combat`。如需审查旧 manifest 保存的历史动作列表，额外传入 `--use-manifest-action-scope`；该模式不代表当前 live runner 的真实动作空间。

输出中包含：

- `messages`: 实际发送给模型的完整 chat messages。
- `prompt_sections.static_system_prompt`: 静态 system prompt。
- `prompt_sections.dynamic_system_prompt`: 历史字段名，当前实际保存稳定 harness contract 文本。
- `prompt_sections.stable_system_payload`: 结构化稳定 contract。
- `prompt_sections.user_payload`: `state_summary`、`compact_evidence`、skills、knowledge ledger 和分层轨迹。
- `prompt_sections.run_context_compression`: 当前使用的 `hierarchical`、`aggressive` 或 `episode` 压缩级别。

真实 run 中，每一步的完整 prompt 也会写入 `context_built` 事件：

```sql
select payload
from trajectory_events
where event_type = 'context_built'
order by id
limit 1;
```

## 当前 Harvest 动作原语

为了让 skill 能沉淀出 procedure，而不是被 worker 宏动作吞掉，harvest 类任务默认暴露更低层动作：

- `scan_blocks`: 扫描附近目标方块，不改变世界。
- `scan_dropped_items`: 扫描附近掉落物，不移动。
- `move_to`: 移动到一个坐标附近。
- `dig_block_at`: 挖指定坐标的方块。
- `wait_ticks`: 等待短时间，用于方块掉落、自动拾取和短暂状态更新。
- `query_inventory`: 验证背包状态。
- `request_visual_snapshot`: 卡住或需要视觉信息时请求画面。

旧的采集宏动作已从 schema、worker、prompt 和测试链路中移除。采集与拾取必须通过扫描、移动、指定坐标挖掘、扫描掉落物、移动到掉落物并等待拾取来完成。

## 预期 Skill 形态

成功完成 `minedojo_harvest_oak_log` 后，理想 skill 应该沉淀完整 procedure，例如：

```json
[
  {"type": "scan_blocks", "args": {"block": "oak_log"}},
  {"type": "move_to", "args": {"position": {"x": 1, "y": 65, "z": 0}}},
  {"type": "dig_block_at", "args": {"position": {"x": 1, "y": 65, "z": 0}, "block": "oak_log"}},
  {"type": "scan_dropped_items", "args": {"item": "oak_log"}},
  {"type": "move_to", "args": {"position": {"x": 1, "y": 65, "z": 0}}},
  {"type": "wait_ticks", "args": {"ticks": 20}},
  {"type": "query_inventory", "args": {}}
]
```

其中具体坐标来自运行时 observation；后续 skill 泛化时应保留“如何选择目标”的规则，而不是固定坐标。
