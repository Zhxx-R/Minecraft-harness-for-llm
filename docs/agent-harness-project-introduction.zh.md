# Minecraft Agent Harness：面向真实游戏环境的可治理与可演化 LLM Agent 基础设施

> 秋招项目介绍长稿。本文采用论文 *Agent Harness for Large Language Model Agents: A Survey* 中的六组件定义 `H=(E,T,C,S,L,V)`：Execution Loop、Tool Registry、Context Manager、State Store、Lifecycle Hooks、Evaluation Interface。

## 一、项目概述

我做的项目叫 **Minecraft Agent Harness**。它的目标不是单纯通过 Prompt 让大模型“会玩 Minecraft”，而是围绕 Agent 在真实、长时、可交互环境中的工程问题，搭建一套能够约束模型决策、执行游戏动作、管理上下文和长期状态、从任务轨迹中沉淀可复用 Skill，并对全过程进行验证和审计的 Agent 运行基础设施。项目的一个核心亮点，是参考 Hermes Agent 将 Skill 作为程序性记忆的思路，在 MineDojo 任务集上实现一条不依赖模型微调或强化学习、而是通过外部技能库持续积累能力的非参数式 Agent 自进化链路。

这个项目解决的核心问题是：LLM 即使具备一定推理能力，直接接入 Minecraft 后仍会遇到输出格式不稳定、动作越权、上下文持续膨胀、环境状态污染、失败后无法恢复、成功判定不可靠，以及每次新任务都从零开始探索等问题。因此，我把模型定位为“受约束且可替换的决策模块”，把执行可靠性和能力积累放在 Harness 层解决。系统的典型链路是：加载 MineDojo 任务 Manifest，重置独立 Minecraft 环境，获取结构化 Observation，构造模型上下文，生成并校验单个 Action，调用 Mineflayer Worker 执行，再把 Action Result、Verifier 结果和完整轨迹写入数据库；成功轨迹和经后续恢复验证的失败经验经过筛选、摘要、去重、版本化与晋升后进入 Skill Library，并在之后的相关任务中作为策略上下文被检索和复用。

项目采用控制面与执行面分离的架构：

- Python/FastAPI 后端负责执行循环、模型路由、上下文、状态、技能、评测和审计；
- Node.js/TypeScript Mineflayer Worker 负责连接真实 Minecraft Server，并把受控动作翻译成游戏操作；
- Backend 与 Worker 通过 WebSocket JSON-RPC 通信，LLM 不直接接触 Mineflayer JavaScript 或 MineDojo Python API；
- PostgreSQL/SQLAlchemy/Alembic 保存 Run、Step、模型调用、错误、记忆、Skill、Checkpoint 和评测记录，Redis 提供训练队列适配；
- React/Vite Dashboard 提供运行列表、逐步轨迹回放、成本与错误分析、Skill 审核和 Creative 人工审核；
- MineDojo 只作为任务定义与评测元数据来源；MineCLIP 作为独立、非权威的视觉评测服务，不进入 Agent 的成功判定权限。

目前项目已将 1,581 条 MineDojo Programmatic Task 和 1,560 条 Creative Task 适配为统一的 Harness Manifest，共 3,141 条；这里的“已适配”表示任务定义和 Schema 链路已经接通，不等于这些任务都已被真实 LLM 成功完成。项目当前是 **单 Agent 系统和并行 Single-Agent 训练平台**，多个 Worker 之间不通信，因此不把它包装成 Multi-Agent 项目。

### 核心亮点：基于 Skill 沉淀的非参数式 Agent 自进化

MineDojo 在本项目中不仅是评测任务来源，也构成了 Agent 持续获得环境交互经验的任务分布。项目参考 Hermes Agent 的程序性记忆思想，但没有让模型随意把一次回答保存成 Skill，而是把 Skill 沉淀实现为一条由 Harness 管理、可追溯的学习闭环：

`MineDojo 任务执行 -> 完整轨迹与 Verifier 证据 -> 成功步骤筛选/失败经验分类 -> Learning Candidate -> 后续成功恢复验证 -> Skill Candidate -> 相似度去重 -> 版本化晋升 -> 按任务检索并注入后续上下文`

Skill 使用 `SkillSpec` 表达，包含名称、版本、触发条件、前置条件、任务 Scope、依赖、策略摘要、参数化计划、恢复策略、来源 Run、来源 Step 范围和 Verifier 证据。成功 Run 只有包含可复用的有效进展动作时才会生成候选；失败 Run 不会直接生成 Skill，而是先排除模型超时、Worker 故障等基础设施噪声，将有长期价值的 Gameplay 失败归一化为 Learning Candidate，并要求后续同 Scope 的成功恢复和 Verifier 证据完成验证。候选还要经过基于动作类型、目标、触发词、任务范围和依赖关系的相似度去重，之后才能进入 `draft、validated、staged、promoted、deprecated` 生命周期。

Promoted Skill 不会作为固定宏替 Agent 直接操作环境，而是根据任务目标和当前 Scope 被检索，以 `strategy_summary、parameterized_plan、recovery_policy` 等形式按需注入 Context；模型仍需结合当前 Observation 选择受约束的原子 Action。整个过程不计算梯度、不修改基础模型权重，也不训练 RL Policy，能力变化发生在 Harness 外部的程序性记忆和技能库中，因此可以将其概括为 **无需微调/RL的非参数式 Agent 自进化**。这一机制主要落在 State Store，但同时贯穿 Execution Loop 的经验产生、Context Manager 的技能消费、Lifecycle Hooks 的门控治理和 Evaluation Interface 的证据验证。

下面按照 Agent Harness 的六个维度展开介绍。

## 二、E：Execution Loop——执行循环与失败恢复

论文将 E 定义为对 observe-think-act 循环、轮次顺序、终止条件和错误恢复的治理。本项目在这一维度实现了真实 Minecraft 环境中的 `reset -> observe -> context -> model -> validate -> act -> verify -> record` 闭环。

执行循环首先通过 `GameRuntime` 抽象统一 `reset、observe、act、snapshot、close`，再由 `ExecutionLoop` 编排每一步。模型不会直接返回自然语言命令，而是输出包含简短决策摘要、证据、知识需求和一个 Action 的 JSON Envelope。动作执行后，Harness 保存 Observation、模型原始返回、Action、Result 和 Verifier 证据，然后决定继续、成功结束、提交外部评测，还是因为预算或故障终止。

为了防止一次模型错误破坏整个任务，我设计了分级恢复机制：

- 非法 JSON、Schema 解析失败或不在允许范围内的 Action 会在进入 Worker 前被拦截；
- Harness 会基于原始上下文、错误信息和当前 Action Allowlist 生成一次受约束的 Repair Prompt；
- 如果修复仍失败，只会在当前工具范围内选择 `query_inventory` 或 `request_visual_snapshot` 等低风险兜底动作，否则明确终止；
- 模型 Provider 的超时、429 和 5xx 支持有界重试、退避和并发上限，并把每次失败计入模型调用与成本审计；
- Action RPC 超时不会被误判为动作失败，而是标记为“结果未知”，补做一次 Observation，并根据动作风险决定是否允许恢复；
- Verifier 超时会重试一次，仍失败则返回 `inconclusive`，不会把基础设施异常写成任务失败；
- Live Runner 可以在 Worker 无响应时重启 Worker 并重新入队，同时保留原始 Attempt 及其错误证据。

终止条件同样由 Harness 控制，包括最大步数、任务运行时间、Runtime 主动终止、受验证的 Success Checker、Action 终止以及 `submit_for_evaluation`。其中 `submit_for_evaluation` 只表示 Agent 请求停止并提交当前轨迹；Programmatic Task 仍需 Verifier 判定，Creative Task 则进入外部评测和人工审核，模型没有自行宣布成功的权限。

在训练编排上，我实现了带任务重试的并行 Single-Agent Runner：任务先按相似度组织为多组 Diversity Wave，一个 Worker 绑定一个独立 Server、RCON Endpoint、Bot Username 和 World Directory。批次开始时冻结 Skill 与 Learning Candidate 快照，所有 Wave 完成后统一处理学习写入。正式批处理支持 Wave 级原子 Checkpoint，恢复时会校验任务计划和快照 Revision；未完成的半个 Wave 安全重跑，已完成部分不会重复结算。

这里有两个明确边界。第一，通用 `ExecutionLoop` 已实现按步保存和恢复 Harness 状态，但它不能恢复完整 Minecraft 世界状态；当前 Live Runner 关闭了单 Run 的步级 Checkpoint，正式长跑主要依赖 Wave 级恢复。第二，项目实现并测试了可选的 Agent Plan 创建与失败后修订模块，但默认实机训练路径仍以 ReAct 为主，没有启用显式 Planner。因此，更准确的表述是“具备可插拔规划能力”，而不是“所有实机任务都由 Planner 驱动”。

## 三、T：Tool Registry——工具治理与动作边界

论文将 T 定义为类型化、可校验的工具目录，以及工具调用的路由和监控。本项目的核心取舍是：不允许模型生成自由代码控制 Minecraft，而是把外部能力收敛为有限、可审计的 Harness Action。

当前共享 Action Schema 包含 23 种动作类型，主要分成三类：

- Runtime Actions：扫描、移动、挖掘、拾取等待、加工物品、放置、装备、交互、消耗物品和有界战斗等会读取或改变世界状态的动作；
- Knowledge Actions：`resolve_terms、get_recipe、retrieve_docs` 三个只读知识工具，由 Backend 内部路由，不发送给 Mineflayer Worker；
- Control Actions：例如 `submit_for_evaluation`，由 Harness 截获并切换到验证流程，不属于游戏动作。

Python 的 Pydantic 模型、共享 JSON Schema 和 Worker 端 Zod Schema 会共同校验 Action Envelope 和 Action Type。具体参数在 Worker Handler 执行前继续做运行时校验，并返回结构化的 `invalid_args` 等错误。也就是说，当前系统已经做到动作类型和调用边界的双端强约束，但还没有把 23 种 Action 的全部 Args 建成完整的静态 Discriminated Union；这一点我会在面试中如实说明。

为了让 Agent 的过程能力真正存在于轨迹中，我移除了早期过粗的采集宏动作，把 Harvest 拆为类似 `scan_blocks -> move_to -> dig_block_at -> scan_dropped_items -> move_to -> wait_ticks -> query_inventory` 的原子序列。这样虽然增加了决策步数，但每个阶段都能单独验证和恢复，也能区分“没有找到目标”“路径不可达”“方块已挖但未看到掉落”“已拾取但 Verifier 未通过”等不同问题，而不是把所有逻辑封装在一个黑盒 Worker API 里。

实时战斗采用不同时间尺度的分工：模型通过 `scan_entities` 选择目标和战术模式，`move_to_and_engage_combat` 在 Worker 内完成有时间上限的动态追踪和攻击，并在目标死亡、低血量、不可达、无弹药、失去视线或超时时把控制权交还模型。该动作不会自动替模型装备或回血，相关决策仍需显式调用 `equip_item` 和 `consume_item`。旧战斗别名保留兼容性但从 Prompt 中隐藏，防止模型选择过时接口。

所有 Action Result 使用结构化失败语义，区分 `target_not_found、no_path、target_unreachable、missing_item、missing_station、no_ammo、timeout、runtime_error` 等状态，并携带 `recoverable`、最新 Observation 和诊断证据。对于挖掘掉落和战斗击杀，系统尽量使用 Minecraft Server 同步的背包变化、Item Entity 和 Mineflayer `entityDead` 事件，不根据方块或怪物类型猜测结果，从而保证后续 Verifier 和 Skill 学习使用的是可追溯证据。

## 四、C：Context Manager——上下文构建、知识检索与长轨迹压缩

论文将 C 定义为对进入模型上下文的信息进行检索、压缩、过滤和优先级管理。Minecraft 任务会产生大量重复 Observation、动作结果和环境元数据，如果直接累积完整历史，模型成本和注意力噪声都会快速上升。因此，我把上下文管理作为独立模块设计。

Prompt 被拆分为三层：

1. 静态 System Prompt：规定 Agent 身份、安全边界、不得编造状态、不得输出 Raw Mineflayer/MineDojo 代码，以及证据不足时的探索原则；
2. 稳定 Harness Contract：包含当前可见动作、参数说明、Knowledge Tool Contract、Runtime Hints、终止协议和结构化输出格式；
3. 动态 User Payload：只放当前 Task、State Summary、Compact Evidence、Task Memory、Task Plan、Promoted Skills、同 Scope Learning Candidates 和 Run Context。

静态规则和稳定 Contract 合并为可复用的 System 前缀，同一 Action Profile 下每轮保持稳定，减少 Prompt 漂移并便于模型服务使用 Prefix Cache。每轮完整 Prompt 仍会写入审计事件，既能复现实验，也能分析某个动作到底受哪些上下文影响。

Run 内轨迹采用分层压缩，且“完整保存”和“模型可见”严格分离：原始 Observation、Decision 和 Action Result 始终完整写入数据库；发给模型时，上一动作保留动作相关的关键证据，最近若干步保留压缩后的导航距离、背包增量、掉落物、加工或击杀结果，更早步骤按 `navigation、collection、processing、combat、interaction` 等语义阶段合并。超过字符预算后，Context 会从 `hierarchical` 降为 `aggressive`，再降为 `episode`，只保留阶段摘要、动作计数、进展信号和最近失败。默认 Run Context 预算为 12,000 字符，其中知识账本最多 3,500 字符，并优先驱逐可重复查询的知识。

知识检索采用 Agent-driven 方式，`auto_retrieve_knowledge` 默认关闭。模型自行判断是否需要查术语、配方或文档，Harness 负责只读边界、来源、长度预算和审计。相同 `Action Type + Args` 的确定性查询使用 Run 内 Exact Cache；结果先作为下一轮 Evidence，再进入低优先级知识账本，空间不足时由完整结果降为摘要，最后允许驱逐并重新查询。当前术语和配方使用确定性查表，文档使用词法匹配；虽然 PostgreSQL 已预留 pgvector 和 Embedding 字段，但不能把当前实现宣传为完整的向量 RAG。

结构化游戏状态是主要观察通道，视觉帧按需注入。模型调用 `request_visual_snapshot` 后，Backend 会捕获可信 Minecraft 窗口并在下一轮构造 Qwen/OpenAI 兼容的多模态 Message；数据库只保存图片路径、尺寸、哈希等审计信息，不重复存储 Base64。Skill 也不是自动执行的宏，而是经过 Scope 过滤后进入 Context 的策略性经验，模型仍需结合当前 Observation 选择原子动作。

该维度仍有明显优化空间。100 任务正式运行产生了约 5,195 万 Token，说明长任务、多次 Retry 和大规模稳定 Contract 的成本仍然很高；当前分层压缩已经解决重复知识查询和历史无限增长的问题，但 Token 效率仍是下一阶段需要通过更细粒度 Context Budget、状态差分和实验对照继续优化的方向。

## 五、S：State Store——持久化状态、任务记忆与技能演化

论文将 S 定义为跨 Turn、可选跨 Session 的任务状态持久化，以及部分失败后的恢复能力。本项目使用 SQL 数据库作为长期状态和审计事实源，而不是把状态只保存在进程内存或 Prompt 中。

当前持久化模型覆盖 `runs、steps、trajectory_events、model_calls、runtime_errors、task_memories、skills、learning_candidates、knowledge_chunks、checkpoints、creative_evaluations、human_reviews` 等实体。`PersistentEvaluationRecorder` 会先记录统一的 Typed Event，再把关键事件派生到便于查询的专用表，因此既保留原始事实，又支持 Dashboard 按 Run、Step、Agent、Error 或评测状态查询。

任务记忆采用 Task/Job 局部 Namespace。失败且可归因的任务会把 Verifier Reason 写入 `task_memories`；同一任务的 Retry 继承该 Namespace，从而能读取前一次失败反思，而不同任务和不同 Job 之间保持隔离。需要注意的是，仓库中的通用 `TaskMemory` 抽象目前仍是占位实现，真实 Live Path 直接通过 SQL `TaskMemoryRecord` 读写，因此更准确的说法是“Live 失败记忆已经落库”，而不是“通用 Memory Service 已完全产品化”。

Skill Library 是项目实现非参数式 Agent 自进化的权威状态源，使用 SQL 记录版本、状态、触发条件、前置条件、参数化策略、恢复策略、来源 Run、来源 Step 和 Verifier 证据。Skill 默认不是固定 Macro：原始 `action_plan` 只用于审计和 Replay，真正给模型的是 `strategy_summary、parameterized_plan、recovery_policy` 等策略信息。这样可以避免把某次 Run 的绝对坐标或偶然环境状态复制到下一次任务，也使 Skill 可以独立于某个模型 Provider 持续积累和迁移。

对于失败经验，我没有采用“失败次数达到阈值就直接写 Skill”的方式，因为模型超时、Worker 异常、随机出生或错误猜测都可能污染长期状态。系统先保留完整失败轨迹，再把有持久 Gameplay 价值的证据归一化为 Learning Candidate，状态经历 `observed -> hypothesized -> corroborated -> validated -> promoted`，也可能进入 `rejected/expired`。只有后续同 Scope 成功 Run 使用匹配的恢复动作并通过 Verifier，Candidate 才会被标记为 Validated，并可能参与 Skill 生成。知识调用只作为带来源的旁证，不能单独把模型猜测提升为世界事实。

并行训练使用 Snapshot Barrier：批次开始时冻结 Promoted Skill 与 Active Learning Candidate，Worker 在批次中看不到彼此刚产生的经验；所有任务完成后，再统一执行失败分类、恢复验证、Skill Candidate 创建、相似度去重和可选 Promotion。Skill 写入以 `source_run_id` 保证幂等，Candidate Signature 使用唯一约束，Promotion 使用数据库行锁，避免恢复或并发 Finalize 生成重复版本。

当前边界是：自动 Promotion 已经过 Verifier、创建策略和去重门槛，但尚未形成系统性的跨 Seed Replay 与泛化复验。100 任务运行导出了 13 个新 Promoted Skill，但它们的 `usage_count` 仍为 0、`last_verified` 为空，所以可以说“完成了有证据的 Skill 生成与治理链路”，不能说“已经证明 Skill 能稳定提升后续任务成功率”。

## 六、L：Lifecycle Hooks——运行治理、安全策略与环境隔离

论文将 L 定义为调用前后的拦截点，用于认证、日志、策略执行和 Instrumentation。本项目在生命周期治理上已经实现了多项具体能力，但其实现目前分布在 Tool Registry、Action Repair、Runtime Decorator、Training Runner 和 Recorder 中，尚未完全收口为统一的 Policy Engine。

在运行前，Harness 根据 Runtime Profile 和 Task Manifest 配置环境。Programmatic Task 可以通过 RCON 清理背包与掉落物、恢复生命和饥饿、设置时间天气、初始化装备、生成目标实体或放置必要方块。每条 Reset 命令、响应、耗时和错误都会进入 `environment_reset` 事件。不同 Worker 生成的实体带有 Owner Tag 和 Task Tag，下一次 Reset 只清理对应 Worker 和任务的资源，避免并行任务相互污染。

Runtime Profile 区分可复现的 Benchmark 环境和带 Fabric/Carpet 便利能力的本地开发环境。Mod 可以帮助观察、暂停威胁、复活或调试，但不能扩展 LLM 可见的 Action Surface，且必须写入 Run Metadata，避免把开发辅助结果与正式 Benchmark 混在一起。Server Pool 还会保存 Worker、Server、端口、RCON 和 World Directory 的一一映射，并给出本地内存预算。

在运行中，Lifecycle 治理还包括模型并发上限、Retry、Step/Runtime Budget、Action Timeout、Worker 健康检查、任务重新入队、Threat Pause、审计身份补全和 Skill 写入屏障。Creative 媒体接口只允许读取 Artifact Root 下白名单扩展名，人工审核提交使用 `expected_version` 做乐观锁，旧版本请求返回冲突，防止并发审核互相覆盖。交接脚本还显式约束 API Key、Minecraft EULA、Loopback 网络暴露和第三方依赖下载。

需要强调的是，`LifecycleHooks.before_action/after_action` 当前默认实现仍是 No-op，只提供了统一拦截接口；系统也尚未完成面向多租户生产环境的通用 Authentication、Authorization 和 Quota Engine。因此，准确的秋招表述应该是：“我实现了环境重置、运行预算、动作门控、故障恢复、审计和人工审核等生命周期治理，并为统一 Hook/Policy 层预留了接口”，而不是“已经完成企业级全套权限系统”。

## 七、V：Evaluation Interface——验证、可观测性与证据回放

论文将 V 定义为以标准结构捕获 Action Trajectory、Intermediate State 和 Success Signal，供 Benchmark、离线分析和可观测平台消费；它与普通日志的区别是，V 不只记录“调用了什么”，还要表达“这一步是否推动了任务目标”。

本项目首先定义了统一 Task Manifest，包含任务来源、类别、Goal、Allowed Actions、Reset Plan、Verifier、Knowledge Tags、Seed 和 Budget 等字段。Programmatic Verifier 支持背包净增、方块放置、实体击杀增量、物品使用增量、生存时间，以及 `all/any` 组合条件。以 Harvest 为例，Verifier 比较初始和最终背包，而不是只看最终是否已有目标物品；以 Combat 为例，优先使用 Native Kill Stat 或 Worker 记录的 `entityDead` 事件，避免把物品掉落或目标离开视野误当成击杀。

执行过程通过 Typed Event 流记录 `run_started、environment_reset、observation、context_built、model_action、knowledge_tool_call、action_result、verifier_result、runtime_error、checkpoint、skill lifecycle、creative evaluation` 等事件。Recorder 同时保存模型原始响应、Token Usage、修复调用、Planner 调用和失败调用，避免成本统计只计算最后一次合法 Action。Dashboard 可以按 Step 聚合并回放 Observation、Context、Model Decision、Action、Result 和 Error，同时保留 Raw Events，支持从某个 Skill 或评测结论追溯到原始证据。

Programmatic Benchmark 会输出 Success Rate、Steps、Invalid Action Rate、Runtime Crash Rate、Token、成本和 Verifier Reason。当前离线质量门实测为：Backend 241 项测试通过，Worker 10 项 Runtime 测试通过，Worker/Frontend TypeScript Typecheck 通过，4 个共享 Schema、10 个 Curated Manifest 和 3,141 个 Executable Manifest 校验通过。这些数字证明代码和数据契约稳定，不代表 LLM 任务成功率。

真实 100 任务正式运行采用 5 个 Worker 和 5 个隔离 Minecraft Server，运行约 12 小时 22 分，共执行 477 个 Attempt，其中 81 个任务发生 Retry；最终 27/100 成功，分布为 Combat 18/30、Harvest 9/57、TechTree 0/13；其余结果为 43 个 Failed（其中 36 个 `verifier_failed`、7 个 `target_not_found`）、20 个 Task Timeout 和 10 个 Runtime Error。全程记录 23,548 次模型调用和约 5,195 万 Token。这个结果一方面证明系统可以在长时间、多 Server、多 Retry 条件下持续运行、持久化和恢复，另一方面也明确暴露出长链路 TechTree、资源获取、模型延迟和 Worker 稳定性仍是当前瓶颈。我会把 27% 作为实验基线，而不会只展示局部成功 Demo。

Creative Task 使用另一条评测链路：可信 Minecraft 窗口录屏、ffmpeg/ffprobe 校验、16 帧重叠窗口抽样、独立官方 MineCLIP Scorer、关键帧选择和 Human Review。项目曾发现旧录屏可能错误捕获桌面而非游戏窗口，因此后续通过 CoreGraphics 锁定真实 Layer-0 Minecraft 窗口，并在录制前后保存截图、尺寸和 SHA-256；Preflight、Postflight 或视频解码验证失败时直接标记 `inconclusive`，不再给错误画面打分。一次可信回归生成约 81.9 秒 H.264 视频，对 164 帧、20 个窗口完成 MineCLIP MPS 前向；由于任务阈值仍未校准，系统保留分数和关键帧，但正确输出 `inconclusive`。当前设计进一步把 Human Review 作为 Creative 成功的唯一权威，MineCLIP 只提供非权威评分和低置信度趋势，避免自动评测反向污染成功标签。

## 八、项目总结

这个项目对我最大的价值，是让我把 Agent 从“一次模型调用”理解成一个完整的运行系统：模型负责在证据和工具范围内做决策，Harness 负责执行顺序、工具权限、上下文预算、状态持久化、故障恢复、成功验证和审计证据。相较于只展示几个成功 Demo，我更关注系统能否在真实环境中长时间运行、失败时保留证据、恢复时不污染状态，以及评测不确定时能否诚实地输出 `inconclusive`。

如果用一句话概括，我会这样介绍：

> 我设计并实现了一套面向真实 Minecraft 的六维 LLM Agent Harness，将不稳定的模型输出约束为可校验的原子动作，并参考 Hermes Agent 的程序性记忆思想，在 MineDojo 任务集上建立“轨迹采集—证据验证—Skill 生成—去重晋升—上下文复用”的学习闭环，使 Agent 无需微调或强化学习即可通过外部技能库持续沉淀能力，同时具备可执行、可恢复、可评测和可追溯的工程基础。

当前已经完成的是单 Agent 执行、并行 Single-Agent 训练、失败学习与 Skill 治理、Programmatic/Creative 评测和 Dashboard；仍在继续攻克的是长链路 TechTree 成功率、Token 效率、跨 Seed Skill Replay、统一 Lifecycle Policy Engine、Headless 视觉采集和真正的 Multi-Agent 协作。把这些边界说清楚，既能体现项目规模，也能体现我对 Agent 工程可靠性和实验有效性的理解。

## 九、面试口径提醒

- 3,141 表示已通过 Schema 校验的任务 Manifest 数，不表示 3,141 个任务均已跑通。
- 10/10 Curated Benchmark 是 Scripted/Deterministic Harness 链路验证，不能当作真实 LLM 成功率。
- 100 任务真实基线是 27/100；其中 Combat 60%，Harvest 约 15.8%，TechTree 尚未成功。
- 当前系统是并行 Single-Agent，不是 Multi-Agent。
- Skill 是上下文中的参数化策略经验，不是默认自动执行的宏。
- 13 个新 Promoted Skill 尚无跨任务 Replay 使用记录，不能宣称已证明泛化增益。
- 当前知识检索以确定性查表和词法检索为主，不能宣称已完成向量 RAG。
- MineCLIP 是辅助信号；未校准或证据不可信时输出 `inconclusive`，Creative 最终成功由人工审核决定。
- Planner、单 Run Checkpoint、统一 Lifecycle Hooks 都有实现基础，但默认 Live Path 的集成程度不同，应按本文边界表述。

## 十、主要核对依据

- 论文六维定义：*Agent Harness for Large Language Model Agents: A Survey*，Definition 2.1，PDF 第 14–15 页。
- 项目架构：`docs/architecture.md`、`README.md`、`docs/adr/0001-runtime-boundaries.md`。
- E/T/C/S/L 核心代码：`backend/src/mc_agent_harness/harness/`、`backend/src/mc_agent_harness/training/live_runner.py`、`workers/mineflayer-worker/src/`。
- V 与 Skill：`backend/src/mc_agent_harness/evaluation/`、`backend/src/mc_agent_harness/skills/`、`backend/src/mc_agent_harness/harness/persistent_recorder.py`。
- 100 任务输出：`week10-recovery-20260719/run-files/runs/formal/week10-24h-5w-20260718T082538Z/week10_formal_batch.json`。
- Creative 可信录屏回归：`runs/week11/vision_clip_regression_20260716T053624Z/`。

## 十一、秋招前建议优先整改的内部问题

以下问题不属于对外项目介绍正文，但会影响项目的安全性和实验可信度：

- P0：正式运行产物当前会写入未脱敏的 PostgreSQL Connection URL。应先轮换相关凭据，再在报告与日志层统一脱敏，并避免把原始内部产物对外分享。
- P0：正式运行报告没有完整固化 Git Commit、工作区状态、模型 Provider/Profile、Temperature 和 Prompt/Manifest Hash，导致“结果对应哪版代码和模型”不能完全复现；应补充统一 Experiment Provenance。
- P1：当前主 CI 包含 Backend Test、Schema Validation 和前后端 Typecheck，但 Worker Runtime Test、Ruff 和真实 E2E 仍未全部纳入；`tests/e2e` 也尚未形成自动化场景。
- P1：100 任务 Worker 日志存在大量依赖弃用警告，造成日志膨胀和信号污染；需要升级相关 Mineflayer 调用或增加有边界的去重与采样。
- P1：补齐工具 Args 的静态 Discriminated Union、统一 Lifecycle Policy Engine、Skill 跨 Seed Replay 和 Creative 校准集，再开展 Harness/Raw/Skill-evolved 的正式对照实验。
