# Minecraft Agent Harness 16 周完整开发计划

## 总览

16 周目标是把当前项目骨架发展成一个完整的工程化 agent 项目：默认主模型为 `qwen3.7-plus`，Mineflayer 是唯一在线执行 runtime，MineDojo 作为任务与评测 provider。项目通过 harness 的 `H=(E,T,C,S,L,V)` 架构展示结构化执行、知识库、记忆、skill 进化、审计和多 agent 编排如何提升 Minecraft 长任务稳定性。

Fabric mods 只作为可选的开发/训练便利 profile 使用，用来辅助观察、暂停、复活控制和本地调试。它们不是 agent action API 的一部分，并且必须写入 run metadata，保证 benchmark 结果能和 vanilla 或最小 runtime 的结果分开统计。

阶段目标：

- Week 1-4：单 agent 可运行内核，知识库前置，打通 worker、模型、日志和持久化。
- Week 5-8：任务集、评测、skill 进化和 MVP dashboard，形成可用于秋招展示的版本。
- Week 9-12：并行训练、creative/MineCLIP、长任务和实验报告。
- Week 13-16：multi-agent 基础设施、协作/对战场景、工程加固和最终展示材料。

## Runtime Profiles

- `benchmark-minimal`：默认可复现评测 profile。它尽量保持 Minecraft 环境接近 Mineflayer runtime 的基础假设，不依赖便利 mod 完成任务。
- `dev-fabric-observation`：可选本地开发和训练 profile，参考 Voyager 的 Fabric 设置。候选 mods 包括 Fabric API、Mod Menu、Complete Config、Multi Server Pause 和 Better Respawn。这个 profile 用于更方便地观察、人工调试、暂停 server 和让 agent 在观察者附近复活。
- Runtime profile metadata 必须包含 Minecraft version、loader/version、mod list、mod config checksum、world seed、game mode、difficulty、LAN port 和 cheats 是否开启。
- 无论使用哪个 profile，worker 和 agent 看到的 harness action interface 都保持一致。Mods 可以改变环境便利性，但不能引入绕过 `ToolRegistry` 的隐藏动作或 API。
- 实验报告必须按 runtime profile 分组或过滤。`dev-fabric-observation` 下产生的 run 不能混入 `benchmark-minimal` 的成功率结论，除非明确标注。

## 实施计划

### Week 1：工程基线与最小知识库

- 建立 CI：Python 编译/测试、TypeScript 类型检查、JSON schema 校验。
- 固化开发环境：Docker Compose、`.env.example`、本地启动脚本、README quickstart。
- 添加第一版 runtime profile 配置文件，并在文档中说明 Fabric mods 只是可选本地辅助，不属于默认 benchmark。
- 建立最小知识库：Minecraft 术语表、canonical item/block/entity IDs、核心合成表、Mineflayer 操作说明。
- 定义 `KnowledgeProvider`：`resolve_terms`、`get_recipe`、`retrieve_docs`，但知识库默认不自动拼接进 prompt。
- 验收：知识库工具被手动调用时，输入 `wooden pickaxe/log/plank/crafting table` 能返回 canonical ID 和 recipe hints。

### Week 2：Mineflayer Worker RPC

- 实现 backend 到 worker 的 WebSocket JSON-RPC：`reset/observe/act/snapshot/close`。
- worker 连接 Minecraft server，并返回 position、health、food、inventory、nearby blocks/entities。
- 实现基础动作：`query_inventory`、`request_visual_snapshot`。
- 记录 worker lifecycle event：connected、spawned、disconnected、error、timeout。
- 连接时记录 runtime profile metadata，包括 LAN port、可获得的 game version 和配置的 mod profile。
- 验收：不调用 LLM，手动 action 可以连接 server 并返回结构化 observation。

### Week 3：单 Agent 执行循环

- 通过 `ModelRouter` 接入 `qwen3.7-plus`，支持 JSON action 输出和 usage 记录。
- 实现 `observe -> context -> model -> action -> runtime -> log`。
- Context 注入 system prompt、task spec、observation、compact evidence、action contract 和 knowledge tool contract；术语解析、recipe hints 和文档片段由模型主动调用知识工具获得。
- `ToolRegistry` 校验 action scope，拒绝 raw Mineflayer JS。
- 验收：agent 能完成一次受控 inventory 查询、一次知识工具调用和简单 mining attempt，并留下完整审计轨迹。

### Week 4：持久化、知识索引与审计

- 加入 PostgreSQL/pgvector、Alembic 和 SQLAlchemy models。
- 表设计：runs、steps、trajectory events、model calls、runtime errors、task memories、skills、knowledge chunks。
- 持久化知识库 chunks、知识工具调用事件和确定性检索来源；pgvector 在 Week 4 作为后续 vector/hybrid retrieval 的基础能力启用，但 Week 4 不依赖 naive embedding-only RAG。
- 实现最小 checkpoint/resume。
- 验收：backend 重启后，历史 run、日志和知识检索来源仍可查询。

### Week 5：Minecraft 动作扩展

- worker 实现 `scan_blocks`、`scan_dropped_items`、`move_to`、`dig_block_at`、`wait_ticks`、`craft_item`、`place_block`、`use_item`、`fight_entity`。
- worker 内部可以使用 pathfinder/collectBlock，但不暴露给 LLM。
- 添加 `dev-fabric-observation` 的可选支持说明，覆盖 Better Respawn 和 Multi Server Pause，并定义只在训练/调试 run 中允许 agent 在观察者附近复活的策略。
- 定义 action timeout、failure reason、recoverable error 和 unrecoverable error。
- 实现简单 verifier：inventory contains、block placed、entity defeated。
- 验收：采木、合成木板、合成木镐、放置方块可端到端运行。

### Week 6：Task Provider 与基础评测

- 实现 `MineDojoTaskProvider`，先覆盖 Harvest、TechTree 和 Combat 小集合。
- 定义 task manifest：goal、allowed actions、verifier、success criteria、knowledge tags。
- 实现 benchmark runner：固定 seed、固定 task set、固定 model profile。
- Benchmark metadata 必须包含 runtime profile、mod list、seed、difficulty 和 respawn policy。
- 输出指标：success、steps、duration、invalid action rate、runtime crash rate、tokens、cost。
- 验收：10 个基础任务可批量运行，并生成 JSON/Markdown 报告。

### Week 7：Skill 进化 MVP

- 扩展 `SkillSpec`：triggers、preconditions、action plan、validation、source run、source step range、task scope、dependencies、status、version。
- 使用 Postgres 作为 skill library 的权威存储。Markdown 文件只作为 promoted 或 staged skill 的 review/export 快照，不作为 runtime source of truth。
- 实现 `draft -> validated -> staged -> promoted -> deprecated` 状态机。
- 建立多级 skill 索引：exact trigger/canonical ID match、task tag match、precondition/action-scope match、dependency match，以及 lexical similarity fallback。
- 默认策略：同一任务失败至少 3 次后，后续成功轨迹可生成 skill candidate。
- Skill promotion 使用数据库锁；training run 读取固定 skill snapshot，保证评测可复现。
- 每次 skill read/write/promotion/export 都记录为 trajectory event，并关联 source trajectory、verifier result 和 replay status。
- 验收：`harvest_log` 可以沉淀 `harvest_oak_log`，promoted skill 会导出 Markdown 供人工 review，新 run 能通过 `execute_skill` 复用数据库中的固定 skill snapshot。

### Week 8：MVP Dashboard 与秋招展示版本

- UI 页面：run list、run detail、event timeline、model calls、runtime errors、skill review。
- 通过 Redis Streams 或 WebSocket fanout 支持实时事件。
- 添加 raw Mineflayer code-generation baseline，必须在 sandbox 中运行，崩溃只影响 baseline。
- 对比 raw codegen、no-skill harness、skill-evolved harness。
- 现场 demo 可以使用 `dev-fabric-observation` 方便观察、暂停和恢复，但 dashboard 必须清楚标注 runtime profile。
- 验收：现场演示同一模型在 harness 下更稳定。

### Week 9：训练调度与并行探索

- 实现 training runner：任务队列与并行执行。
- 每个 task 使用独立 memory namespace。
- 加入 resource budget：max steps、max tokens、max runtime、worker concurrency。
- 训练任务可以选择 `dev-fabric-observation` 提升调试效率，但每个 run 都要保存 profile metadata 和 profile-specific metrics。
- Redis 负责 queue/run state；Postgres 负责审计和最终状态。
- 验收：5-10 个任务并行执行时，不产生 skill 写入冲突或日志错乱。

### Week 10：Programmatic Catalog、差异化并行训练与 Skill 治理

- 从 MineDojo 官方 task description 文件导入完整 programmatic task catalog。
- 区分 curated executable manifests 与完整 catalog：curated manifests 用于 CI 可执行验证；完整 catalog 用于任务选择、分桶和后续 live training。
- 实现任务相似度计算：goal 文本、category/family、knowledge tags、allowed actions、verifier target 和 MineDojo metadata。
- 加入 diversity-aware batch planner，使同一训练 epoch 中并行 worker 优先选择低相似度任务。
- 在 promotion 前加入 skill candidate deduplication：比较 action plan、triggers、task scope、dependencies 和 name，避免重复 skill 污染 promoted skill library。
- 加入 `SkillCreationPolicy`、`LearningCandidate` 状态机和 `SkillSummarizer`：失败次数只触发 scoped hypothesis review，不直接生成 skill；基础设施/随机环境噪声被拒绝，knowledge call 只能提供引用，后续同作用域成功恢复并通过 verifier 后才允许进入 skill。当前先使用确定性 evidence-backed summarizer，后续 LLM reviewer 只负责草拟策略文案，不能绕过 schema、来源、dedup、verifier 和 replay 门槛。
- 加入 live parallel programmatic training：多个隔离 Mineflayer workers 执行可运行 programmatic manifests，验证成功条件，写入 task-local 失败记忆，并创建/提升 skill candidates。
- 保留 epoch 语义：worker 在一个 epoch 内读取固定 skill snapshot，skill promotion 在 candidate 收集和去重后统一处理。
- 固化当前知识库检索边界：`resolve_terms/get_recipe` 使用确定性别名与 canonical ID 查表，`retrieve_docs` 使用 lexical overlap；所有结果记录 retrieval mode、source snapshot 和 matched aliases，避免把当前实现误认为语义 RAG。
- 继续 Skill 质量治理：replay/verifier 回归测试、metrics、deprecation 和 progressive disclosure。
- 验收：本地 catalog 包含 1581 个 programmatic tasks；并行 batch 能尽量避开高相似任务；2-3 个 live Mineflayer training workers 可以执行 programmatic tasks 并更新 skills，同时避免重复 promotion。

### Week 11：Creative Task 与 MineCLIP

- 添加 MineCLIP scorer adapter，输入截图/视频帧与 creative prompt。
- 添加 creative task manifest：prompt、frame sampling policy、score threshold、calibration examples。
- MineCLIP 是外部 evaluator，不作为 agent 自评依据。
- UI 展示 creative score、key frames 和 score trend。
- 验收：至少 3 个 creative task 可自动评分，并保留校准阈值。

### Week 12：长任务与上下文压缩

- 定义长任务：资源链到石器/铁器、简单庇护所或下界前置链路。
- 实现 hierarchical context：current state、recent steps、task summary、retrieved memory、skills、以及 agent 主动调用的 knowledge tool results。
- 把自动知识拼接迁移为 agent-driven knowledge retrieval：模型通过 `resolve_terms/get_recipe/retrieve_docs` 决定是否检索，harness 负责 allowlist、长度预算、来源标注、prompt-injection 清洗和审计。
- 将知识库升级为多级索引文档：canonical item/block/entity index、recipe dependency graph、Mineflayer API/action guide、Minecraft Wiki 章节索引。默认仍先走精确/lexical 检索；当文档规模扩大后再加入 BM25 + 可选 embedding reranker 的 hybrid retrieval，并在审计中明确区分检索策略。
- 实现 stuck detection：重复位置、重复失败 action、verifier 无进展。
- stuck 时允许注入视觉帧并触发反思式重规划。
- 验收：100+ step 任务可以运行、可中断恢复、日志可 replay。

### Week 13：Multi-Agent 基础设施

- 加入 agent identity、role、message schema、inbox/outbox。
- 使用 Redis Streams 实现 leader/collector/crafter/combatant 异步通信。
- 定义 multi-agent lifecycle hooks：权限检查、消息校验、共享状态锁。
- 支持 typed team messages，避免自由格式消息污染 context。
- 验收：两个 agent 可异步协作完成资源收集与合成。

### Week 14：Multi-Agent 场景

- 协作：分工采集、合成、简单建造。
- 对战：两个 agent 或两队 agent 在隔离 arena 中执行受限动作集；LLM 不做每 tick 决策，而是输出战术意图、目标选择、装备策略和中断条件，worker 内的快速 combat controller 执行追击、瞄准、攻击、格挡、撤退、吃食物等低延迟动作。
- 引入实时对战 baseline：Mineflayer PVP/Pathfinder/Armor Manager/Auto Eat/Hawkeye 等插件或等价自研 controller；把 rule-based/reactive controller、LLM tactical controller、planning-acting 双线程 controller 作为对比组。
- 参考调研文档：[实时对战 Agent 方案调研](../research/realtime-combat-agent-research.zh.md)。
- 角色扮演：固定 role prompt、typed messages、shared world state。
- 评测 multi-agent overhead：success rate、communication rounds、cost、conflicts、deadlocks/timeouts。
- 验收：至少一个协作场景和一个对战或角色扮演场景可演示。

### Week 15：工程加固与安全

- 加固 sandbox：raw codegen baseline 和未来代码型 skill 都必须隔离运行。
- 实现 permission scopes：runtime actions、knowledge scopes、skill write scopes。
- 加入 rate limits、cost caps、run cancellation、worker health checks。
- 接入 OpenTelemetry 做服务级可观测性：traces、spans、latency/error metrics 和可选 OTLP export；SQL audit logs 仍作为 agent replay 与 skill provenance 的 source of truth。
- 将 OpenTelemetry 的 trace/span id 与 `run_id`、`step_index` 关联，方便从生产链路追踪跳回 dashboard replay 证据链。
- 加固知识工具：source allowlist、snapshot checksum、retrieval budget、返回片段清洗、工具调用审计和 regression query set，防止不可信文档或过长检索结果污染 prompt。
- 加固实时对战 controller：动作频率上限、冷却时间、health/food 安全阈值、LLM timeout 时的规则兜底、arena reset 隔离和反作弊/服务器规则兼容检查。
- 固定并校验支持的 runtime profiles，包括 Fabric loader/mod versions 和 config checksums；当实际环境与声明 run config 不一致时 fail fast。
- 添加失败注入测试：worker crash、invalid LLM JSON、DB restart、Redis restart。
- 验收：常见失败不会导致服务整体崩溃，run 状态可恢复或可解释。

### Week 16：最终报告、演示与发布

- 固定实验集：20-40 个 programmatic tasks、3-5 个 creative tasks、1 个长任务。
- 输出最终报告：baseline 对比、skill learning curve、cost、crash rate、long-task trajectory、multi-agent demo。
- 完成 README：架构图、quickstart、核心设计、实验结果、项目亮点、roadmap。
- 准备面试材料：3 分钟 demo、10 分钟技术讲解、tradeoff/problem list。
- 验收：从零启动服务、运行任务、查看 UI、展示报告的链路可复现。

## 公共接口

- `GameRuntime`：`reset(task_spec)`、`observe()`、`act(action)`、`snapshot()`、`close()`。
- `KnowledgeProvider`：`resolve_terms(task_text)`、`get_recipe(item_id)`、`retrieve_docs(query, limit, scope)`。
- `KnowledgeToolDispatcher`：`dispatch(tool_call)`，只读执行知识工具，执行 allowlist、scope、budget、source citation 和 audit。
- `TaskProvider`：`list_tasks()`、`load_task(task_id)`、`verify(run_state)`、`score_creative(frames, prompt)`。
- `ModelRouter`：`generate_action(messages, model_profile, response_schema)`，记录 tokens、latency、cost、vision usage。
- `RuntimeProfile`：`load(profile_id)`、`validate(connection_info)`、`record_run_metadata(run_id, profile_metadata)`。
- `SkillLibrary`：`search(query, scope)`、`get(name, version)`、`create_candidate(run_id)`、`promote(candidate_id)`、`deprecate(skill_id)`、`export_markdown(skill_id)`。
- `AgentMessageBus`：`send(message)`、`receive(agent_id)`、`ack(message_id)`。
- `TrajectoryEvent`：统一记录 model calls、action calls、runtime events、verifier results、knowledge retrieval、skill read/write 和 agent messages。

## 测试计划

- Unit tests：action schema、term resolver、recipe lookup、tool registry、context budget、model parser、skill state machine、skill indexes、message schema。
- Contract tests：backend-worker JSON-RPC、invalid action、timeout、disconnect、worker crash、snapshot failure。
- Integration tests：采木、合成木板/木镐、放置方块、采石、简单战斗、skill 复用。
- Runtime profile tests：声明的 profile metadata 会被持久化，profile mismatch 会 fail fast，mod-assisted run 会被标注，并且 mods 不会向 agent 增加隐藏动作。
- Knowledge tests：任务目标中的 Minecraft 专有名词必须解析到 canonical IDs、recipes 和 required tools。
- Knowledge retrieval tests：固定 query 集覆盖 exact ID、别名、recipe graph、wiki section、Mineflayer action guide；每次检索记录 strategy、source、matched aliases、truncation 和 checksum。
- Evaluation tests：固定任务集比较 raw codegen、no-skill harness、skill-evolved harness 和 multi-agent harness。
- Long-run tests：100+ step 任务支持 checkpoint、resume、stuck detection 和 context compression。
- Real-time combat tests：controller tick loop 不依赖 LLM 延迟；LLM timeout 时 rule-based fallback 能继续防御/撤退；arena replay 能审计战术意图、controller action 和伤害事件。
- Creative tests：MineCLIP 阈值经过人工样本校准，key frames 和 scores 可审计。
- Regression tests：每个 promoted skill 都保存 source trajectory、verifier result、replay record、固定 snapshot metadata 和 Markdown export checksum。

## 假设

- 项目按单人 16 周开发节奏设计；Week 8 是秋招可展示 MVP，Week 16 是完整版本。
- `qwen3.7-plus` 是默认主模型；其他模型保持可插拔，但不是前 8 周重点。
- Mineflayer 是唯一在线 runtime；MineDojo 只提供任务元数据与评测。
- Fabric mods 是可选开发/训练辅助。它们用于观察、server 暂停和复活控制，但不暴露给 LLM；除非明确标注，否则不纳入默认 benchmark 结论。
- 知识库从 Week 1 开始接入，agent 不靠猜 Minecraft 内部 ID 完成任务。
- Web search 默认关闭，只在后期作为受控 ablation。
- 初期 skill 是结构化动作序列，权威版本存储在 Postgres。Markdown export 只作为人工 review 快照；代码型 skill 是后期优化，必须经过 sandbox、replay 和 verifier 审批。
