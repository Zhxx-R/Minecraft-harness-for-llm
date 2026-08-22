**State Store**
   
   它是系统的长期记忆和审计事实源。Run、Step、Trajectory Event、Model Call、Runtime Error、Checkpoint、Skill、Learning Candidate、Creative Evaluation 都落库。

   Skill 自进化主要发生在这一层：成功轨迹提炼 Skill Candidate，失败轨迹先进入 Learning Candidate，必须被后续同 Scope 成功恢复验证后，才可能参与 Skill 生成。

一、State Store 的框架定位
整个系统里 State Store 处在这里：
Execution Loop
  -> 产生 observation / action / result / verifier / error
      -> State Store 持久化

Batch / Training Runner
  -> 读取 runs / steps / events
      -> 失败分类
      -> Learning Candidate
      -> Skill Candidate
      -> Skill Promotion

Dashboard
  -> 读取 State Store
      -> 回放轨迹
      -> 查看错误
      -> 审核 Skill
      -> 查看评估结果

State Store 同时服务三类模块：
    执行恢复
    学习演化
    审计展示


二、State Store 存什么
原始执行事实
  runs
  steps
  trajectory_events
  model_calls
  runtime_errors
  checkpoints

长期学习状态
  task_memories
  learning_candidates
  skills

知识与评估
  knowledge_chunks
  creative_evaluations
  human_reviews

三、Run 是什么
Run 是一次完整任务尝试。
Run ID: run_001
Task: harvest_oak_log
Status: succeeded / failed / task_timeout / runtime_error
Started at: ...
Finished at: ...
Task Spec: 本次任务配置
resumed_from_checkpoint_id

Manifest 是任务文件
task_spec 是 Run 里的 JSON 配置
RunRecord 是数据库表

四、Step 是什么
Step 是一次 observe-action-result 循环。：它把一次任务拆成可回放的粒度。
Run 是任务级记录，Step 是决策级记录。每个 Step 保存 observation、action 和 action_result，方便重放 Agent 每一步为什么这么做。


五、Trajectory Event 是什么
Trajectory Event 是统一事件流，保存比 Step 更细的运行事实。它让 Dashboard 和后续学习模块可以从任意结论追溯到原始证据。
一次 Step 内部还会发生很多事件：
context_built
model_action
invalid_action
model_repair_attempt
knowledge_tool_call
action_result
verifier_result
runtime_error
checkpoint_saved
skill_lifecycle_event

六、Model Call 是什么
Model Call 记录模型调用本身。Model Call 独立落库是为了区分模型决策质量和环境执行质量，方便统计 token、成本、非法动作率和 repair 成功率。
模型有没有输出非法 JSON
token 用了多少
repair 前后的输出是什么
模型超时几次
某次错误是不是模型造成的

七、Runtime Error 是什么
Runtime Error 记录底层执行异常。Runtime Error 用来隔离基础设施故障，避免把 worker、server、RPC、model provider 的问题误写成 Agent 的游戏经验。

reset 失败
observe 失败
worker timeout
Mineflayer disconnected
RCON command failed
server crash

八、Checkpoint 是什么
Checkpoint 用于恢复执行状态。

它保存的不是完整 Minecraft 世界，而是 Harness 的运行状态：
next_step_index
task_spec
task_memory
previous_step
run_context
recent step summary
注意这个边界非常重要：
Checkpoint 恢复 Harness 状态
不等于恢复完整 Minecraft 世界状态

如果 Minecraft 世界被破坏，通常需要更高层的 reset 或 batch 重跑。
面试说法：
Checkpoint 解决的是 Harness 层的断点续跑，比如恢复 step index、任务记忆和压缩轨迹；它不是完整世界快照。真实环境一致性主要靠 reset、server 隔离和 wave 级别重跑保证。

九、Task Memory 是什么
Task Memory 是任务级记忆。

比如某次任务失败了：
Failed because zombie target was unreachable behind terrain.
下一次 retry 同一个任务时，可以把这条记忆注入 context：
Previous attempt failed because target was unreachable. Relocate before scanning again.


十、Learning Candidate 是什么
你前面已经问过，这里和 State Store 连起来讲。
Learning Candidate 是从失败 Run 中提取出的失败经验假设。
它会落库，因为它需要跨 Run 验证。

失败 Run
  -> 读取 runs / steps / trajectory_events
  -> 过滤 runtime_error / model_timeout 等噪声
  -> 找 gameplay failure
  -> 生成 Learning Candidate
  -> 后续同 Scope 成功 Run 验证它

失败不是直接进 Skill
失败先进入候选池
后续成功验证后才升级

十一、Skill 是什么
Skill 是长期可复用经验。
Skill Library 是 Agent 自进化的长期状态源。Skill 不是自动执行脚本，而是经过 Verifier 证据支持的参数化策略记忆。通常包括：

name
version
status
trigger
preconditions
scope
dependencies
strategy_summary
parameterized_plan
recovery_policy
source_run_id
source_step_range
raw_action_sequence
verifier_evidence
usage_count
last_verified
这个经验来自哪次 Run？
是哪几个 Step？
那次任务是否真的成功？
Verifier 证据是什么？
原始 action 序列是什么？

状态可能是：
draft
validated
staged
promoted
deprecated
模型使用 Skill 时，Context Manager 只注入摘要：
strategy_summary
parameterized_plan
recovery_policy


十二、Skill 自进化怎么发生在 State Store
完整闭环是：
Execution Loop 产生轨迹
  -> State Store 保存 Run/Step/Event
      -> Batch 结束后分析成功和失败 Run

成功 Run:
  -> 选择有效进展 Step
  -> 去掉无关探索
  -> 去掉绝对坐标
  -> 总结参数化策略
  -> 生成 Skill Candidate

失败 Run:
  -> 过滤基础设施噪声
  -> 生成 Learning Candidate
  -> 等后续同 Scope 成功 Run 验证
  -> 验证后参与 Skill Candidate

Skill Candidate:
  -> 相似度去重
  -> 版本化
  -> staged/promoted
  -> 写入 skills 表
也就是说：
State Store 不只是存结果
它提供 Skill 学习所需的证据链


十四、Creative Evaluation 和 Human Review
Programmatic task 可以用程序判断：
背包增加
击杀增加
目标状态完成
Creative task，比如建筑任务，没那么容易程序判断。
Creative Evaluation 可能包括：
录屏路径
关键帧数量
MineCLIP 分数
阈值
calibration_status
result

Human Review 包括：
人工审核结论
证据版本
reviewer decision
comments

MineCLIP 是辅助信号
Human Review 才能作为 creative task 更可信的最终判断


十五、哪些是表，哪些不是表
你可以这样记：
是表：
runs
steps
trajectory_events
model_calls
runtime_errors
checkpoints
task_memories
skills
learning_candidates
knowledge_chunks
creative_evaluations
human_reviews
不是单独表，通常是字段或文件：
task_spec
  -> runs.task_spec JSON 字段

task manifest
  -> tasks 目录下的 JSON/YAML 文件

action scope
  -> manifest/task_spec 里的 allowed_actions 字段

trace
  -> trace_id/span_id 字段，分布在 runs/steps/events/model_calls 等表

verifier
  -> manifest/task_spec 里的配置 + 代码里的 success_checker 实现

observation/action/action_result
  -> steps 表里的 JSON 字段，也会进入 trajectory_events



十六、State Store 的兜底设计
State Store 相关的兜底主要有几个。
第一，基础设施错误不进入 Skill 学习：
runtime_error
model_timeout
verification_inconclusive
这些不会直接生成 Learning Candidate。

第二，Skill 写入要幂等：
source_run_id 保证同一个 run 不重复生成同一批 skill

第三，Learning Candidate 要去重：
failure signature 防止重复创建同一种失败经验

第四，Skill Promotion 要并发控制：
数据库事务
行级锁
状态检查
避免多个 worker 同时把相似 skill 晋升。

第五，完整事实保留，模型上下文压缩：
数据库存完整轨迹
Prompt 只放压缩摘要
这让后续审计不会丢证据。


十七、面试怎么讲
你可以这样说：
State Store 是我设计的长期状态和审计事实源。Execution Loop 每一步都会把 observation、action、action result、model call、verifier result 和 runtime error 结构化记录下来，形成可回放的 trajectory。Run 和 Step 记录任务级和步骤级事实，Trajectory Event 保留更细的事件流，Model Call 单独记录模型输出和 token 使用，Runtime Error 用来隔离基础设施故障，Checkpoint 用来恢复 Harness 层执行状态。  
在 Skill 自进化上，State Store 提供证据基础：成功 Run 会经过有效进展步骤筛选，归纳为可参数化的 Skill Candidate；失败 Run 不会直接变成 Skill，而是先过滤 model timeout、worker error、verifier inconclusive 等基础设施噪声，只把有长期价值的 gameplay failure 归一化为 Learning Candidate。Candidate 必须在后续同 Scope 成功 Run 中被恢复策略验证，并通过 Verifier，才可能参与 Skill 生成。Skill 写入还会做相似度去重、版本管理、source_run_id 幂等和事务/行级锁并发控制，保证并行训练时 Skill Library 不被污染。

最关键的一句话：
State Store 的价值不是“把日志存起来”，而是把 Agent 的每一步环境交互变成可审计事实，再从这些事实中安全地产生长期技能记忆。


简历问题：
流程整个：
这套 Skill 演化不是让 LLM 自己总结经验，而是 Harness 基于结构化 trace 做规则化后处理。模型只负责每一步选择 action；worker 返回结构化 action_result，Verifier 判断任务是否成功；批次结束后，Harness 再根据失败 run 和成功 run 生成 Learning Candidate 与 Skill Candidate。

失败 run -> 提取稳定 gameplay failure -> 生成 Learning Candidate
成功 run -> 验证同 scope 的 Learning Candidate -> 生成 Skill Candidate，如果这个成功 run 验证过 Learning Candidate，就把这条恢复经验写进 skill 的 strategy_summary / recovery_policy / validation
Skill Candidate -> 查重/审核/可选自动晋升 -> Promoted Skill


举例：
失败 run：move_to oak_log 返回 no_path
生成 Learning Candidate：遇到 no_path 时不要反复走同一坐标，应重新 scan 或换 reachable target

后续成功 run：scan_blocks 找多个 oak_log -> 第一个 no_path -> 换第二个 -> move_to 成功 -> dig_block_at 成功 -> verifier 通过

这时 Learning Candidate 被 validated；
同时这个成功 run 可以生成 Skill Candidate；
Skill 的 recovery_policy 会包含：no_path 时换目标或使用 nearest_reachable_position。

总述：
Skill 演化分成失败学习和成功技能两条线。失败 run 只生成 Learning Candidate，记录某个任务 scope 下的稳定 gameplay failure；这些失败诊断来自 worker 返回的结构化 action_result，比如 no_path、nearest_reachable_position、suggested_affordances，都是项目自定义的 Harness 诊断字段。Learning Candidate 初始只是待验证假设，只有后续同 scope 成功 run 使用恢复动作并通过 Verifier，才会被 validated。

Skill Candidate 来自成功 run。系统读取 StepRecord，规则化筛选成功的可复用动作，把原始 action_plan 保留作审计，再按动作类型生成 parameterized_plan，避免复用旧世界绝对坐标。如果该成功 run 验证过 Learning Candidate，恢复经验会写入 skill 的 recovery_policy 和 validation。最后经过相似度去重和人工/配置晋升，才成为 promoted skill。整个过程主要是规则化后处理，不是 LLM 自己宣布学会。


1. 失败run做什么、怎么生成的 Learning Candidate？
这套 Skill 演化不是让 LLM 自己总结经验，而是用规则化后处理从 trace 中提炼。失败 run 只保留稳定的游戏层失败，比如 no_path、missing_ingredient、target_unreachable，排除模型超时和 worker 异常这类基础设施噪声；这些失败会生成 Learning Candidate，表示“这个 scope 下存在一种失败模式”。
Learning Candidate 本身不会直接变成 skill，必须等后续同 scope 的成功 run 提供恢复证据，并通过 Verifier，才会被标记为 validated。

2. 失败诊断从哪里来：action_result
失败诊断主要来自 action_result。
  比如 move_to 失败时，worker 可能返回：
  navigation_failure_reason：失败原因，比如 no_path、path_timeout
  nearest_reachable_position：路径规划认为最近可达的位置
  suggested_affordances：下一步可尝试的能力，比如重新 scan、dig_block_at 清障、place_block 辅助移动
底层 Mineflayer/pathfinder 只负责执行和路径计算，结果整理成统一 action_result 诊断字段。Harness 后处理和下一轮模型上下文都依赖这些结构化诊断，而不是让模型凭感觉猜失败原因。

move_to 的导航底层用的是 Mineflayer pathfinder，不是我们从零实现路径规划。Harness 每轮先 observe 当前环境，模型基于观察结果输出结构化 move_to action；Python 校验后通过 RPC 发给 TypeScript worker，worker 调 pathfinder 规划路径并驱动 bot 移动。项目主要做的是动作协议、超时控制和失败诊断：worker 会把 pathfinder 的失败原因、路径摘要、最近可达点和预定义恢复建议整理成统一 action_result，再进入下一轮上下文，让模型基于证据决定重扫、换目标或清障，而不是凭感觉猜。

pathfinder 负责产生底层路径事实，比如是否找到路、路径最后节点、规划摘要和执行异常；项目 worker 再把这些事实加工成统一的 Harness 诊断字段。像 nearest_reachable_position 的值来自 pathfinder，但字段设计属于我们；navigation_failure_reason 和 suggested_affordances 则是项目根据路径结果和规则生成的，不是 Mineflayer 官方直接返回。

3. Learning Candidate 怎么被验证
Learning Candidate 的验证依赖后续同 scope 的成功 run。系统不是直接相信失败假设，而是要求后续 run 用一组恢复动作最终通过 Verifier；只有这样，失败经验才会从“观察到的失败”变成“已验证的恢复经验”。

4. 成功 run 怎么生成 Skill Candidate
成功 run 会进入 Skill Candidate 生成流程，但不是成功就一定生成。
成功 run 结束后，系统会读取 StepRecord，筛选出真正有复用价值的成功进展动作，比如扫描、移动、挖掘、加工、放置、战斗等；目标不匹配的实体动作、没有成功进展的轨迹、简单配方知识已经覆盖的单步流程，都会被跳过。然后系统把源轨迹泛化成参数化策略：原始 action_plan 保留给审计，但 prompt 里使用 parameterized_plan，把旧世界里的绝对坐标替换成 selected_block_position、nearest_reachable_candidate 这类当前环境占位符，要求模型在新任务中重新基于 observation 和 scan 选择目标。

5. Learning Candidate 怎么进入 Skill
Learning Candidate 本身不会直接变成 Skill。失败经验只作为恢复策略进入 skill，skill 的主体仍然来自成功轨迹。也就是说，失败告诉系统“哪里容易错”，成功告诉系统“怎么做能完成”。


某个成功 run 验证了它；
这个成功 run 又生成了 Skill Candidate；
于是这条 validated failure lesson 会被写进 Skill 的 strategy_summary、recovery_policy 和 validation。

6. Skill Candidate 怎么变成 Promoted Skill
生成 Skill Candidate 后，还要做两步：
  先查重：第一层是数据库身份唯一性，name + version 不能重复；后比较动作类型、目标、trigger、task scope、dependencies 和 name，相似度过高0.82就不新增。
  再晋升：默认不自动晋升；如果开启 auto_promote，或人工审核通过，才把 status 改成 promoted。

7. SkillSpec.status 的生命周期状态
draft：草稿状态。刚从成功 run 的 trace 里生成出来，还不能认为稳定可复用。
validated：已验证。说明这个 skill 有验证证据，比如来源 run 成功、verifier 通过，或者恢复经验被后续成功 run 证明过。
staged：待发布/灰度状态。介于 validated 和 promoted 之间，可以理解为“准备上线进技能库，但还没正式给 agent 广泛使用”。
promoted：正式晋升状态。只有 promoted skill 才会作为可召回技能进入正常上下文，给模型当经验参考。
deprecated：废弃状态。说明这个 skill 不再推荐使用，可能因为过时、重复、效果不好，或者被新版本替代。

8. skill结构：
概念上，一个 Skill 可以分成四块。第一块是身份信息，比如 name 和 version，用来标识技能版本，并作为数据库唯一键；真正防止语义重复时，还会比较动作类型、目标和触发词等相似度(0.82)。
第二块是status用于启用/禁用/待审核
第三块是召回信息，包含任务类型、目标 ID、触发词和所需动作，用于和当前任务做相关性匹配。
第四块是使用与审计信息：strategy_summary、parameterized_plan 和 recovery_policy 会被压缩注入上下文
第五块provenance为该skill对应的run的信息，用于人工审计和来源追踪：source_run_id、source_step_range、source_evidence、verifier_stats、validation 和 metrics 用来追踪这个技能来自哪次 run、效果如何、是否可信。

八、完整例子
一次 harvest oak_log 任务失败，最后一步 move_to 返回 no_path。Harness 从 action_result 里看到这是稳定导航失败，于是生成 Learning Candidate：在 harvest/oak_log 这个 scope 下，move_to 可能出现 no_path，需要后续成功 run 验证恢复方法。
后来另一个同 scope 任务成功了：模型先 scan_blocks 找到多个 oak_log，第一个目标 no_path 后换了另一个目标，move_to 成功，dig_block_at 成功，Verifier 检查背包里有 oak_log。系统就把 scan_blocks、move_to、dig_block_at 提取成 recovery_actions，把 Learning Candidate 标记为 validated。
同时，这个成功 run 会生成 Skill Candidate：原始 action_plan 保留审计，parameterized_plan 写成“扫描当前世界的 oak_log、选择可达目标、移动过去、挖掉并验证背包”；如果这个 candidate 不重复并通过晋升，就成为 promoted skill。后续模型看到类似任务时，会收到这个 skill 的 strategy_summary 和 recovery_policy，比如 no_path 时不要重复同一坐标，要重新扫描或换可达目标。