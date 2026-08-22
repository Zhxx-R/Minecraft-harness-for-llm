# Week 7 开发文档

## 目标

Week 7 实现第一版数据库驱动的 skill 进化 MVP。Skill library 的权威版本在 SQL 数据库中；Markdown 文件只是 review/export 快照。Skill 是结构化 harness action 序列，不是 raw Mineflayer JavaScript。

## 已交付变更

- 扩展 `SkillSpec`：
  - `validation`
  - `source_run_id`
  - `source_step_range`
  - `task_scope`
  - `dependencies`
  - `metrics`
- 更新 `skill.schema.json` 和 schema validation 的代表样例。
- 用数据库版实现替换占位 `SkillLibrary`。
- 增加 skill lifecycle 操作：
  - `search(query, scope, limit)`
  - `get(name, version)`
  - `create_candidate(run_id)`
  - `promote(candidate)`
  - `deprecate(skill_id, version, reason)`
  - `export_markdown(skill_id, version)`
- promotion 使用 SQLAlchemy `with_for_update` 表达数据库行锁语义。
- 当存在 source run 时，skill lifecycle 会写入 trajectory events：
  - `skill_candidate_created`
  - `skill_promoted`
  - `skill_deprecated`
  - `skill_exported`
  - 可选 `skill_read`
  - 可选 `skill_search`
- 增加确定性的多级 skill 检索：
  - exact trigger/canonical id match
  - task tag/scope match
  - action-scope match
  - dependency match
  - name/description lexical fallback
- `ContextManager` 会注入 promoted skill summaries，采用 progressive disclosure。Prompt 只拿到摘要和 action types，不直接拿完整 action plan。

## 存储模型

沿用现有 `skills` 表作为权威存储：

```text
skills.name
skills.version
skills.status
skills.spec
skills.source_run_id
```

扩展字段都存放在 `skills.spec` 中，所以 Week 7 不需要新增数据库 migration。这样能兼容 Week 4 的持久化迁移，同时支持更完整的 skill metadata。

Markdown export 由 `SkillLibrary.export_markdown(...)` 生成，不作为 runtime source of truth。

## Candidate Creation

`create_candidate(run_id)` 会读取一个已持久化的成功 run：

1. 加载 `RunRecord`。
2. 加载 `action_result.ok == true` 的成功 `StepRecord`。
3. 把 step actions 转成结构化 `action_plan`。
4. 抽取 triggers、task scope、dependencies、preconditions 和 source step range。
5. 保存 draft `SkillRecord`。
6. 在 source run trajectory 中记录 `skill_candidate_created`。

这是 Week 7 MVP 版本。完整的失败轮次策略，即“同一任务失败 `>=3` 次后，后续成功轨迹生成 candidate”，可以在训练调度阶段基于同一个 `create_candidate` 方法实现。

## Retrieval

默认只检索 `promoted` skills：

```python
await library.search(
    "collect oak_log for harvest",
    scope=SkillSearchScope(
        task_tags=("minecraft:harvest",),
        canonical_ids=("oak_log",),
        allowed_actions=("mine_block",),
    ),
)
```

这对应 Week 10 的 progressive disclosure 方向：context 先注入 skill 摘要；只有当 `execute_skill` 或 planner 明确选中某个 skill 时，才加载完整 action plan。

## 验证

运行 Week 7 测试：

```bash
cd backend
.venv/bin/python -m pytest tests/unit/test_skill_library.py tests/unit/test_context_manager.py
```

运行完整校验：

```bash
make ci
```

当前预期结果：

```text
backend tests pass
worker typecheck pass
frontend typecheck pass
```

## 当前边界

- `execute_skill` 尚未实现。Week 7 能创建、promote、检索和导出 skill；真正复用执行在下一步。
- 失败轮次触发还没有自动调度。当前 library 支持从成功 run 创建 candidate，训练调度器后续决定何时调用。
- 代码型 skill 不在 Week 7 范围内。当前 skill 只是结构化 action sequence。
