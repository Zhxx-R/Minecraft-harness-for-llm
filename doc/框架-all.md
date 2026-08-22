一、宏观框架
我做的是一个面向开放世界任务的 Agent Harness，重点解决 LLM 在真实环境中执行任务时的工具约束、上下文管理、状态持久化、错误恢复、任务评估和经验沉淀问题。

用户/任务集
   ↓
Task Provider / MineDojo Catalog
   ↓
Execution Loop
   ↓
Context Manager ← Knowledge Store / Skill Library / Task Memory
   ↓
Model Router / LLM
   ↓
Tool Registry / Action Contract
   ↓
Mineflayer Worker
   ↓
Minecraft Server
   ↓
Observation / Action Result
   ↓
Verifier / Evaluation
   ↓
State Store / Trace / Skill Learning
   ↓
Dashboard / Audit / Replay

二、六大模块在系统里的位置
1.Execution Loop：Agent 的主循环
这是大脑调度器。
它负责一轮一轮地执行：
observe 环境观察
→ build context 构建上下文
→ call model 调模型
→ validate/repair action 校验或修复动作 ?
→ execute action 执行动作
→ observe result 观察结果
→ verify 判断任务是否完成
→ record trace 记录轨迹
→ checkpoint 必要时保存断点

2.Tool Registry：模型和真实世界之间的安全边界
怎么把不稳定的 LLM 输出变成稳定、可控、可验证的系统行为？
LLM 不能直接写 Mineflayer 代码，也不能随便调用游戏 API。
它只能输出类似：
{
  "type": "scan_blocks",
  "args": {
    "block": "oak_log",
    "max_distance": 12
  }
}
然后后端用 Pydantic 校验，worker 用 Zod 校验，最后 Mineflayer worker 才真正执行。

3.Context Manager：决定模型每一步看到什么
所以 Context Manager 会把上下文分成三层：
静态 System Prompt
稳定 Harness Contract
动态任务信息

动态信息包括：
当前任务目标
当前环境状态
最近几步 action/result
压缩后的历史轨迹  //?
task memory     //?
检索到的 knowledge
相关 skill
learning candidate   //?
它的作用是：让模型“够用地知道当前局面”，但不被无关历史淹没。

4.State Store：长期状态和审计中心
每个 run、step、model call、action result、runtime error、checkpoint、skill、learning candidate 都会落库。
这很重要，因为 agent 系统不能只看最后成功失败，它要能回答：
模型为什么做这个动作？
当时看到了什么？
动作执行成功了吗？
失败原因是什么？
Verifier 怎么判断？
这个 skill 是从哪条轨迹来的？
所以 State Store 是整个项目的“黑匣子记录仪”。

5.Lifecycle Hooks：治理与拦截点
这一块目前项目里还比较薄，但设计上很重要。
它应该负责在关键节点做拦截：
before_model_call
after_model_call
before_action
after_action
before_verifier
after_verifier
before_skill_write
after_skill_write
可以做：
安全限制
动作审批
日志审计
异常恢复
prompt 改写
成本限制
worker 健康检查
你面试时可以说：当前项目已有基础 hook 和部分治理逻辑，但后续可以进一步抽象成 middleware pipeline。

6.Evaluation Interface：判断 Agent 到底有没有完成任务
Agent 不能自己说“我完成了”。
所以项目里有 Verifier。
比如：
采集任务：背包里目标物品数量是否增加
战斗任务：目标实体击杀数是否增加
科技树任务：是否使用/合成了目标物品
生存任务：是否存活指定时间
这解决的是 Agent 项目里非常重要的问题：
如何客观评估一个 agent 的行为结果，而不是依赖模型自评？


三、完整 Pipeline 流程
假设任务是：采集 1 个 oak log。
完整流程是这样：
1. Task Provider 读取任务
   task_id = harvest_oak_log
   goal = collect 1 oak_log
   allowed_actions = scan_blocks, move_to, dig_block_at, wait_ticks, query_inventory
   verifier = inventory_delta(oak_log >= 1)

2. Execution Loop 创建 run
   生成 run_id
   初始化 runtime
   reset Minecraft 环境
   记录 run_started 事件

3. Runtime observe
   Mineflayer worker 返回当前状态：
   位置、血量、背包、附近方块、附近实体等

4. Context Manager 构建 prompt
   包含：
   当前任务
   当前 observation
   允许动作列表
   上一步结果
   相关 skill
   相关 learning candidate
   action contract

5. Model Router 调 LLM
   模型输出一个结构化 action：
   scan_blocks(oak_log)

6. Action Repair / Validation
   如果 JSON 格式错了、动作类型不允许、参数不合法：
   尝试修复或 fallback 到安全动作

7. Tool Registry 分发动作
   如果是 knowledge action，走知识库
   如果是 runtime action，发给 Mineflayer worker

8. Mineflayer Worker 执行
   scan_blocks 返回附近 oak_log 坐标

9. Execution Loop 记录 step
   observation、action、action_result、model_call 都写入 trace

10. 下一轮
   模型根据 scan 结果选择 move_to 某个 oak_log 附近

11. 再下一轮
   模型执行 dig_block_at

12. 再下一轮
   模型 wait_ticks 或 scan_dropped_items，等待掉落物拾取

13. Verifier 检查
   比较初始背包和当前背包：
   oak_log 数量是否 +1

14. 如果成功
   run.status = succeeded
   记录 verifier_result
   进入 skill candidate 生成逻辑

15. Skill Learning
   系统从成功轨迹中提取可复用策略：
   scan target block
   move near reachable target
   dig concrete block
   wait/pickup
   verify inventory delta

16. Dashboard 展示
   可以查看 run、step replay、model calls、runtime errors、skill 来源

这就是整个闭环。
四、这个项目最核心的思想
你要抓住这几个关键词：
LLM 不直接控制世界
Harness 负责约束和治理
Action 是强类型协议
Trace 是一等公民
Verifier 决定任务结果
Skill 是可审计经验，不是黑盒宏脚本
失败经验需要后续成功验证
尤其是这句话很适合面试说：
这个项目的核心不是让模型一次性规划出完整脚本，而是让模型在 harness 约束下逐步调用原子工具。每一步都有 observation、action、result 和 verifier evidence，因此整个 agent 行为是可审计、可中断、可恢复、可复盘的。

五、接下来怎么学
我建议我们后面按这个顺序学：
先学整体架构和主流程
目标：你能 3 分钟讲清楚这个项目。

学 Tool Registry
目标：你能讲清楚 LLM action contract 怎么保证安全和类型一致。

学 Execution Loop
目标：你能讲清楚一轮 agent step 发生了什么。

学 Context Manager
目标：你能讲清楚 prompt 怎么分层、怎么压缩历史、怎么注入 skill。

学 State Store
目标：你能讲清楚 trace、checkpoint、model call、runtime error 为什么要落库。

学 Skill Evolution
目标：你能讲清楚成功轨迹怎么变 skill，失败轨迹怎么变 learning candidate。

学 Evaluation
目标：你能讲清楚 verifier 怎么判断任务成功，为什么不能让模型自评。

学 Dashboard / 后端接口
目标：你能面后端岗位时讲 API、数据库、审计和可观测性。

最终你需要形成两套话术：
Agent 岗话术：tool calling、context、memory、evaluation、skill learning。
后端岗话术：FastAPI、SQLAlchemy、异步执行、worker RPC、持久化、dashboard、任务调度。
下一步我们可以先从 项目整体架构图 + 3 分钟面试讲法 开始，把它变成你秋招能直接背熟又能展开的版本。