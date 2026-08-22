Mineflayer 是现成的 Node.js Minecraft bot 库，但我们的 Harness 主体是 Python，所以项目没有让 Python 直接操作 Minecraft，而是在 Node/TypeScript 侧写了一个 worker。这个 worker 内部调用 Mineflayer，外部接收 Harness 定义的动作 JSON，比如 move_to、dig_block_at、scan_blocks。Python 侧的 MineflayerClient 只是 RPC 客户端，负责把动作发给 worker、等待结果和处理超时。这样 Python 负责决策和记录，TypeScript 负责真正执行 Minecraft 操作。







1. ActionRepairPolicy
流程是：
    模型输出 ActionDecision → Harness 解析 JSON → Harness 校验 action 是否在允许列表、参数是否合法 → 如果错，把错误和坏输出拼成 repair_request 再问模型 → 如果模型修复成功，用修复后的 ActionDecision → 如果还失败，Harness 自己生成 fallback action。
这里哪些是模型产出：
    第一次 ActionDecision 是模型产出；修复后的 ActionDecision 也是模型产出。
哪些是 Harness 产出：
    repair_request 是 Harness 产出；校验错误是 Harness 产出；fallback ActionDecision 是 Harness 产出。


action_result：
    不是所有 action_result 完全同 schema；项目人为规定了公共骨架，然后每类 action 自己加字段。
    公共骨架是人为规定的，worker 里有两个统一函数：
    success(bot, actionType, details) → { ok: true, action_type: actionType, ...details, observation: observe(bot) }
    failure(bot, actionType, errorCode, message, recoverable, details) → { ok: false, action_type: actionType, error_code, message, recoverable, ...details, observation: observe(bot) }

    例如 scan_blocks 成功返回大概是：
    {
        "ok": true,
        "action_type": "scan_blocks",
        "query": "dirt",
        "max_distance": 12,
        "blocks": [
            {
            "name": "dirt",
            "position": {"x": 1, "y": 64, "z": 2},
            "distance": 3.4,
            "can_dig": true
            }
        ],
        "observation": {}
    }

    dig_block_at 成功返回大概是：
    {
        "ok": true,
        "action_type": "dig_block_at",
        "block": "dirt",
        "position": {"x": 1, "y": 64, "z": 2},
        "block_before": "dirt",
        "block_after": "air",
        "block_removed": true,
        "held_item": null,
        "estimated_dig_time_ms": 750,
        "inventory_delta": {"dirt": 1},
        "spawned_drops": [],
        "drop_observation_status": "inventory_gained",
        "drop_observation_ms": 500,
        "drop_evidence_source": "minecraft_server_entity_packets_and_inventory",
        "observation": {}
    }




plan：
1. planner
planner 是可选的高层计划模块，它也会调用模型，但产物不是 action，而是 TaskPlan。
plan 只是放进 context 里给模型参考，模型下一轮仍然必须自己输出一个 action。
plan 写“先找树，再采 oak_log，再合成 planks”；模型下一轮不能直接执行整个 plan，它只能输出 scan_blocks 或 move_to 或 dig_block_at 其中一个。
流程是：
任务刚开始 → planner 看 task_spec、observation、task_memory、allowed_actions → 生成 TaskPlan → TaskPlan 放进后续 context 给 action 模型参考。
TaskPlan 一行说明：
TaskPlan 是高层策略提示，比如目标、已知对象、当前阶段、开放问题、失败恢复策略，不是要直接执行的动作脚本。
这里哪些是模型产出：
TaskPlan 是 planner 调用模型产出的。

哪些是 Harness 产出：
planner prompt、fallback plan、是否 revise 的判断是 Harness/Planner 代码产出。
举例：
任务是 craft wooden pickaxe，planner 可能产出“先确认背包，有木头则合成木板和木棍，没有木头则扫描树并采集”，但真正执行时模型仍然每轮只能输出 query_inventory、scan_blocks、dig_block_at 这种单个 action。
什么时候 revise：
move_to no_path、scan_blocks 没结果、scan_entities 没结果、战斗目标丢失时，planner 可以修订计划。
为什么要有：
长任务需要一点高层方向，但系统又不想让计划变成不可控脚本。



5. creative task 有两条 MineCLIP 路线。
MineClipScorer.score(frames, prompt, negative_prompts) 的输入是：它会把图片 base64 后发给独立 MineCLIP HTTP 服务：POST /score
    frames：16 张 Minecraft 画面帧，bytes
    prompt：目标文本，比如 "a small dirt pyramid"
    negative_prompts：负样本文本列表，用来做对比

prompt, negative_prompts：
    MineCLIP 不是只看“像不像目标文本”，它会把目标 prompt 和 negative prompts 放在一起做对比。
    正 prompt 定义目标，负 prompt 提供对照物，让 MineCLIP 的概率更有判别意义。
16 帧 Minecraft 画面 + 目标文本 + 若干负样本文本 → MineCLIP 输出目标文本相对负样本的匹配概率。

第一条是在线 progress_feedback，执行过程中的在线视觉反馈
它发生在任务执行过程中，只是辅助反馈。
流程：
creative task 启动 → CreativeProgressMonitor 开始后台截图 → agent 执行动作 → 如果动作是 place_block/dig_block_at/use_item 这类重要动作 → 系统排一个 MineCLIP scoring job → 后台等够 16 帧 → MineCLIP 打分 → 下一轮 observation 里带 creative_progress → ExecutionLoop 记录 mineclip_progress_feedback。
这里的关键点：
progress_feedback 不阻塞 action。意思是：
agent place_block 后，runtime 立刻返回；MineCLIP 评分在后台慢慢做。

    {
        "latest": {
            "job_id": "mineclip-progress-xxx",
            "status": "completed",
            "action_type": "place_block",
            "score": 0.62,
            "score_delta": 0.05,
            "baseline_score": 0.40,
            "trend": "improving",
            "confidence": "low",
            "advisory_only": true,
            "success_authority": "human_review",
            "frame_window": {
            "frame_count": 16
            },
            "summary": "MineCLIP advisory after place_block..."
        },
        "pending_jobs": 0,
        "captured_frames": 64,
        "buffer_ready": true,
        "advisory_only": true,
        "success_authority": "human_review"
    }
解释：
scoring job：后台评分任务的排队单，Harness 自己在 CreativeProgressMonitor 里维护的一个 ProgressCheckpoint。
    创建：creative task 运行中，agent 成功执行重要动作后创建。
    重要动作默认是：place_block、dig_block_at、use_item。它们会明显改变画面或作品状态。
    创建 scoring job 后，不会卡住 action：
        agent 执行 place_block → runtime 返回 action_result → monitor.checkpoint 创建 scoring job 放进队列 → action_result 里加 creative_progress_job → ExecutionLoop 继续跑。

    后台有一个 _score_loop 专门处理队列：
        从 queue 取一个 checkpoint → 等 ring buffer 里有足够的动作后画面 → 取最近 16 帧 → 调 MineCLIP scorer.score → 得到 MineClipScore → 转成 latest_feedback。

完整 MineCLIP 在线反馈流程：
    creative task reset → monitor.start → 后台 sampler 每秒约 2 帧截图 → 帧进入 ring buffer → 先排 baseline job 打初始分 → agent 正常跑 ExecutionLoop → 成功执行 place_block/dig_block_at/use_item → monitor.checkpoint 排 scoring job → action_result 带 creative_progress_job → 后台 scorer 等够动作后的帧 → 取 16 帧发给 MineCLIP HTTP 服务 → 返回 MineClipScore → 转成 latest_feedback → 下一次 observe 把 latest_feedback 放进 creative_progress → ExecutionLoop 记录 mineclip_progress_feedback → ContextManager 把 creative_progress 放进 state_summary/compact_evidence，模型下一轮可以看到趋势。

第二条是任务结束后的 creative evaluation。
流程：
    任务跑完 → 收集录屏或帧目录 → 按策略采样多个 16 帧窗口 → 每个窗口调用 MineCLIP 打分 → 对所有窗口求平均分 → 和校准阈值比较 → 生成 evaluation report → 保存关键帧/最终帧/score_trend → 进入 human review。
    
    {
        "success": false,
        "inconclusive": false,
        "type": "creative_mineclip",
        "task_id": "creative:24",
        "prompt": "build a small dirt pyramid",
        "score": 0.58,
        "score_threshold": 0.70,
        "aggregation": "trajectory_mean",
        "frame_count": 120,
        "window_count": 12,
        "score_trend": [],
        "key_frames": [],
        "final_frame": {},
        "checks": []
    }
这条更像最终评估流程，但项目里仍然强调：
human_review 是最终权威。
因为 MineCLIP 分数不是绝对可靠。

然后 ExecutionLoop 做两件事：
    看到 observation 里的新 creative_progress → 记录 mineclip_progress_feedback
    看到 action_result 里的 creative_progress_job → 记录 mineclip_progress_requested

任务：build a small dirt pyramid → runtime reset，CreativeProgressMonitor 开始采样画面 → 模型每轮照常输出 ActionDecision，比如 place_block → runtime 执行 place_block 并返回 action_result → wrapper 发现这是重要动作，排 MineCLIP 评分 job → 后台收集 16 帧并打分 → 下一轮 observe 时 observation 带 creative_progress → ContextManager 把它放进 state_summary/compact_evidence → 模型看到趋势 improving/stable/regressing 后继续搭建 → 结束后外部 CreativeTaskEvaluator 用录屏/帧序列做更完整 MineCLIP 评估 → human review 最终确认。

ProgressCheckpoint 是后台评分用的动作后标记；
creative_progress_job 是 action_result 里的排队回执；
creative_progress 是 observation 里的评分反馈结果。

图片传递：
因为 HTTP/JSON 不能直接稳定传“图片二进制 bytes”，所以要把每张图片 bytes 转成 base64 字符串，再塞进 JSON 发给 MineCLIP 服务。
1. Minecraft 窗口画面被截图成图片文件。
2. Harness 读取图片文件，得到图片 bytes。
3. CreativeProgressMonitor 把这些图片 bytes 暂存在 ring buffer 里。
4. 当需要评分时，从 ring buffer 里取 16 张连续图片。
5. MineClipScorer 把这 16 张图片 bytes 分别 base64 编码。
6. MineClipScorer 组装 JSON 请求：
{
  "frames": ["第1张base64", "第2张base64", "...第16张base64"],
  "prompt": "目标描述",
  "negative_prompts": ["负面描述1", "负面描述2"]
}
7. Harness 通过 HTTP POST 发给 MineCLIP 服务的 /score 接口。
8. MineCLIP 服务收到 JSON 后，把 base64 字符串解码回图片。
9. MineCLIP 模型拿 16 帧图片 + prompt + negative_prompts 做视觉文本匹配。
10. MineCLIP 服务返回分数 JSON。
11. Harness 把返回 JSON 解析成 MineClipScore。
12. 在线反馈场景下，再把 MineClipScore 转成 creative_progress.latest，下一轮 observation 给模型看。



简历：
1. 术语
Pydantic 是 Python 侧结构校验，负责解析模型输出的 ActionDecision 和 HarnessAction；
JSON Schema 用来约束模型应该输出的结构；
Zod 是 TypeScript worker 入口校验，防止非法 RPC 请求进入 Mineflayer 执行层。
两边都校验，是因为 Python 和 worker 是两个独立进程，不能只信一边。




二. 动作：
第一类是 Runtime Actions，用来操作 Minecraft 环境，比如扫描方块、移动、挖方块、等待、加工物品、放置方块、装备、使用物品、消耗物品和战斗；
第二类是 Knowledge Actions，用来查术语、配方和文档；
第三类是 Control Actions，目前核心是 submit_for_evaluation，用来提交当前任务状态给 Verifier 或外部评估器。
【
    如果有 success_checker：调用 verifier 检查当前 run_state。
    如果 verifier 成功：submission accepted，run 结束。
    如果 verifier 不通过：submission rejected，返回原因，让模型继续执行。
    如果是外部评估任务：接受提交，但标记需要 external evaluation / human review。
    如果没有配置 evaluator：可以结束，但结果是 unverified。】

动作白名单：
采集类任务可能只开放 scan_blocks、move_to、dig_block_at、query_inventory、wait_ticks、submit_for_evaluation；知识查询任务会开放 resolve_terms、get_recipe、retrieve_docs；

. Tool Registry 解决的核心问题是什么？
它解决的是模型和真实游戏执行之间的安全边界问题。模型不能直接写 JavaScript 或调用 Mineflayer API，只能输出项目定义的 HarnessAction；ToolRegistry 校验这个 action 是否在当前任务允许范围内，再决定交给知识工具、控制逻辑或 Mineflayer worker 执行。

1. 为什么要定义原子动作，不直接用 Mineflayer API？
原始 Mineflayer API 太底层，模型直接调用会不可控，也不方便审计和恢复。封装成 scan_blocks、move_to、dig_block_at 这类原子动作后，每一步都有明确输入、输出和 action_result，Harness 可以校验、记录、重试、做 verifier，也能限制模型能力范围。

2. 跨语言动作闭环
完整链路是：模型先输出 ActionDecision，Python 侧用 Pydantic 解析出 HarnessAction；ToolRegistry 检查这个 action 是否在当前白名单里；
如果是 Knowledge Action，就由 Python 内部知识工具处理；
如果是 Runtime Action，就通过 Python MineflayerClient 以 RPC 发给 TypeScript worker；
worker 收到后用 Zod 再校验 action 结构，然后调用 Mineflayer 执行，最后把 action_result 返回给 Python，进入 Recorder 和下一轮上下文。

3. 两边校验：
Python 校验模型输出和任务白名单，TypeScript worker 校验执行入口。Python 发到 worker 的 action 是不是 worker 能执行的格式

LLM -> HarnessAction JSON -> Python Harness 校验 -> TypeScript worker 校验 -> worker handler -> Mineflayer bot API -> Minecraft 世界

4. submit_for_evaluation
第一条，模型主动提交：
LLM 看到证据足够 -> 输出 submit_for_evaluation -> Harness 调 Verifier -> 成功则结束，失败则把原因返回给模型继续跑。
第二条，Harness 每步自动检查：
如果 ExecutionLoop 配了 success_checker，每步 action 后 Harness 会自动调用 Verifier；如果 Verifier 判断任务已经完成，就直接停止，不需要等模型 submit。

位置：LLM 输出一个 ActionDecision之后


5. 白名单 Action 是怎么控制的？
每个 run 都有当前启用的 action 集合，ToolRegistry 只允许模型选择这些动作。模型如果输出未启用动作，会被判非法并进入 action repair。这样可以按任务、实验阶段或安全策略限制模型能力，例如采集任务只开放扫描、移动、挖掘、查背包和提交评估。

6. action分层：
   - Runtime Actions：真的操作 Minecraft，比如移动、扫描、挖掘、合成、战斗
   - Knowledge Actions：查询术语、配方、文档
   - Control Actions：提交评估、终止等控制动作

  
7. knowledge
    1.resolve_terms → 把任务里的自然语言词汇解析成 Minecraft 规范 ID【任务说 wood，但执行动作不能传 wood，必须传 oak_log 这类具体 ID，所以先用 resolve_terms 消歧。】
    2.get_recipe → 查某个目标物品需要什么材料、什么工作站、产出数量。
    3.retrieve_docs → 按问题检索本地知识文档片段，数据库 【当术语和配方不够时，检索本地知识库里的说明文档。】

    resolve_terms：走术语/别名映射，把自然语言词转成 canonical id。
    get_recipe：走配方索引，用物品 id 查配方。
    retrieve_docs：从数据库 chunk 里做关键词/文本重叠匹配，按相关性返回文档片段。

    KnowledgeChunkRecord 这是 RAG-like 的本地/数据库词法检索，还不是向量 RAG。
        documents、terms、recipes 转成 KnowledgeChunk，然后 upsert 到数据库。
        表里：
            id
            source
            title
            content
            tags
            metadata
            enabled
            version

    Knowledge 的来源分三层：
    第一层是项目手写的 minimal 知识，用来补齐任务里最常见但数据集不好直接决策的默认规则；第二层是从 minecraft-data 结构化数据，主要提供物品、方块、实体 ID 和配方；第三层是代码里手写的补充文档和别名映射，比如常见掉落物获取方式、wood 默认选 oak_log、planks 默认选 oak_planks。

    手写主要补了什么：
    手写知识主要补三类：常见模糊词到具体 ID 的默认映射，比如 wood -> oak_log、planks -> oak_planks；常见物品获得方式，比如 feather 来自 chicken，porkchop 来自 pig；还有少量任务高频的操作提示，帮助模型在配方数据之外知道下一步该查什么或做什么。

    为什么要手写：
    手写部分主要解决数据集不直接覆盖的决策问题。比如自然语言里的 wood 默认用 oak_log、planks 默认用 oak_planks；任务说 obtain feather，配方数据里查不到，因为 feather 不是合成出来的，所以需要补充“feather 通常来自 chicken”这类获得方式文档。

8. 知识库怎么检索的？为什么不向量化？
9. action_result 怎么构成的？
不同 action 来源不同：
Runtime Action：由 TypeScript Mineflayer worker 生成。它参考 Minecraft 当前状态、Mineflayer API 返回、pathfinder 结果、背包变化、执行异常、动作后的 observe，整理成 JSON。比如 move_to 会参考路径规划结果、path reset、最近可达点、最终位置等。

Knowledge Action：由 它参考知识库里的 terms、recipes、docs，返回解析结果或文档片段。

Control Action：由 Harness 评估逻辑生成。它参考 verifier / external evaluator 的结果，返回 submission 是否 accepted、task_success、reason 等。


action_result 里的信息来源不是单一 API，也不是全靠 observe。分几类：
Minecraft 当前状态：主要来自 observe(bot)，比如位置、背包、附近方块、实体、血量。
Mineflayer API 返回：比如装备、挖掘、合成、放置、使用物品这些动作执行成功/失败的信息。
pathfinder 结果：只和导航有关，比如 move_to 的路径规划、最后可达节点、path reset、no_path。
背包变化：通常是 action 前后都读一次 inventory，然后 worker 自己算 delta，比如少了木板、多了 crafting_table。
执行异常：来自 Mineflayer/pathfinder/worker 捕获到的错误，比如 timeout、dig_error、recipe_not_found。
动作后的 observe：很多 runtime action 执行完会再调用一次 observe(bot)，把最新环境状态附到 action_result.observation 里。
    
10. action原子动作：：
根据 Minecraft 常见任务，把能力定义成结构化原子动作。
比如移动类的 move_to 只暴露目标坐标和容忍距离，路径规划交给 pathfinder；
观察类的 scan_blocks/query_inventory 把附近方块和背包转成结构化证据，比如附近有没有 oak_log、背包里有什么、地上有没有掉落物。；
交互类的 dig_block_at/craft_item/place_block 要求具体物品 ID、位置和数量；，要求模型基于 observation 选择具体目标，避免输出“弄点木头”这种不可执行指令。
战斗类的 scan_entities/equip_item/engage_combat 把发现目标、装备和攻击拆成受控步骤。先观察实体，再选择装备和目标，最后由 worker 执行接近和攻击，并返回击杀、受伤、目标丢失等结果。
这样模型负责决策，Harness 和 worker 负责校验、执行和返回可审计结果。