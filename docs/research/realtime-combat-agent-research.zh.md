# 实时对战 Agent 方案调研

调研日期：2026-07-02

## 结论

实时对战不适合让远程 LLM 每 tick 直接控制。可行架构是分层控制：

- 慢层：LLM 负责战术意图、目标选择、装备策略、队伍通信、撤退/追击条件。
- 快层：worker 内的 deterministic 或轻量模型 controller 负责追击、瞄准、攻击、格挡、吃食物、走位和短周期中断。
- 审计层：记录 LLM tactical intent、controller state、tick-level action summary、damage/kill/heal events、timeout fallback。

这与当前项目的 harness 目标一致：LLM 保持策略自主，harness 提供低延迟、可审计、可恢复的执行基础设施。

## 公开方案

### Mineflayer 插件生态

Mineflayer 官方 README 列出了一组可组合插件，包括 Pathfinder、statemachine、Armor Manager、PVP、Auto Eat、Auto Crystal、Tool、Hawkeye、Projectile、Movement、Collect Block 等。PVP 插件定位是给 Mineflayer bot 增加基础 PVP/PVE 能力，并依赖 Pathfinder；statemachine 插件用于用有限状态机组织复杂 bot 行为。

参考：

- Mineflayer plugin list: https://github.com/prismarinejs/mineflayer
- Mineflayer PVP: https://github.com/PrismarineJS/mineflayer-pvp
- Mineflayer StateMachine: https://github.com/PrismarineJS/mineflayer-statemachine

对本项目的启发：

- `fight_entity` 不应该只是一次 RPC 调用；它需要变成 worker 内可中断的 combat controller。
- LLM 看到的是战术级 action，例如 `engage_target(target, policy, stop_conditions)`，而不是每 tick 攻击/转向。
- controller 每 50-200ms 自主执行，并把摘要写回 observation。

### Hierarchical Language Agent

HLA 面向实时人机协作游戏，核心是 Slow Mind、Fast Mind、Executor 三层：强 LLM 做意图推理，轻量模型做 macro action，脚本策略把 macro action 转成 atomic action。论文明确指出 LLM API 与复杂 prompt 带来高延迟，不适合直接用于游戏实时交互。

参考：

- Paper: https://arxiv.org/abs/2312.15224
- Project/GitHub: https://github.com/HosnLS/Hierarchical-Language-Agent

对本项目的启发：

- Week14 对战应实现 `Slow Tactical Planner -> Combat Controller -> Mineflayer primitive`。
- `qwen3.7-plus` 可以先只作为 Slow Planner；Fast Mind 可以暂时不用小模型，先用规则状态机实现。

### Parallelized Planning-Acting for Multi-Agent LLM Systems in Minecraft

该工作直接针对 Minecraft 多 agent，提出 planning thread 与 acting thread 并行：LLM 规划线程持续观察并写入 single-slot action buffer，执行线程运行 skill library，并支持 interrupt。论文还描述了 centralized memory、主动/被动通信，以及基于 Mineflayer PrismarineJS 的综合 skill library。它的实验环境保持服务器实时运行，不因 LLM 调用暂停。

参考：

- Paper: https://arxiv.org/html/2503.03505v2

对本项目的启发：

- 对战不能用当前串行 `observe -> model -> act` 循环；需要 `planner_loop` 和 `controller_loop` 解耦。
- LLM timeout 不应该阻塞 combat controller；controller 继续执行上一条有效战术或进入 fallback。
- 中断条件应成为 action schema 的一部分，例如 `interrupt_if: low_health, target_lost, teammate_down, stuck, enemy_too_far`。

### PillagerBench / TactiCrafter

PillagerBench 是面向 Minecraft 实时 competitive team-vs-team 的 LLM 多 agent benchmark，提供可扩展 API、多轮测试和 rule-based built-in opponents；TactiCrafter 使用人类可读战术、自博弈和对手策略适应。

参考：

- Paper: https://arxiv.org/abs/2509.06235
- Code: https://github.com/aialt/PillagerBench

对本项目的启发：

- Week14 的对战场景应先有 rule-based opponents，避免只能 agent 自己和自己比较。
- 评测指标要包含 win rate、survival time、damage dealt/taken、kill/death、communication rounds、LLM latency、controller tick lag、fallback 次数。
- 可以把 TactiCrafter 的“human-readable tactics”作为 skill/memory 格式参考，但不要直接把固定动作轨迹当 skill。

### VillagerAgent / TeamCraft

VillagerAgent 和 TeamCraft 主要偏协作，不是实时 PVP，但它们强调 multi-agent 中的任务依赖、状态管理、部分可观测和协作通信。这些能力对团队对战同样必要。

参考：

- VillagerAgent: https://aclanthology.org/2024.findings-acl.964/
- TeamCraft: https://teamcraft-bench.github.io/

对本项目的启发：

- team state manager 要独立于单个 agent context。
- 对战通信必须 typed，避免自由文本把 teammate 的 prompt 污染。
- 对战策略评估要区分个体 controller 能力和团队通信/分工能力。

## 推荐落地方案

### Phase A：Rule-Based Combat Controller

- worker 新增 `start_combat_controller`、`update_combat_policy`、`stop_combat_controller`。
- controller 内部处理 pathfinder、aim、attack cooldown、shield、eat、retreat。
- observation 增加 combat summary：target、distance、line_of_sight、health_delta、damage_events、controller_state。
- LLM 只决定是否开战、目标是谁、策略是什么、何时中断。

### Phase B：LLM Tactical Planner

- action schema 增加 `combat_tactic`：
  - `target_selector`
  - `engagement_style`
  - `equipment_policy`
  - `retreat_policy`
  - `team_message`
  - `interrupt_conditions`
- planner 每 1-5 秒或关键事件触发，而不是每 tick 调用。
- controller 每 tick 或每数 tick 运行。

### Phase C：Team Arena Benchmark

- RCON/reset 创建隔离 arena。
- rule-based opponent 作为 baseline。
- 评估单挑、2v2、护送/抢点等小场景。
- 所有事件写入 replay：LLM prompt/action、controller state transition、damage timeline、server reset command。

## 不建议的方案

- 不建议让 LLM 输出 Mineflayer JS 或每 tick movement/attack。延迟、错误恢复和安全边界都不可控。
- 不建议把 `mineflayer-pvp` 直接暴露给 LLM 当万能工具。它可以作为 worker 内实现组件，但 harness action 仍应是受限 schema。
- 不建议在 Week10 programmatic training 阶段把 combat 作为主要验收指标。combat 更适合 Week14 独立 arena + controller benchmark。
