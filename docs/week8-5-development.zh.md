# Week 8.5 开发文档：Audit Replay 与证据链视图

## 范围

Week 8.5 把 Week 8 dashboard 从“审计表浏览”升级为“按 step 回放的证据链视图”。这次不改执行循环、不改数据库 schema、不改 worker、不改 skill promotion 逻辑。

目标是让一次真实 run 能按下面的顺序解释清楚：

```text
Observation -> Context -> Model -> Action -> Runtime Result -> Errors
```

原始 timeline 仍然保留，保证完整可审计。

## 后端

新增接口：

```http
GET /api/runs/{run_id}/replay
```

实现位置：

```text
backend/src/mc_agent_harness/api/routes/dashboard.py
```

该接口读取：

- `runs`
- `trajectory_events`
- `steps`
- `model_calls`
- `runtime_errors`

然后按 `step_index` 聚合，返回：

- run 元数据
- run-level 事件，例如 `run_started` 或 reset 阶段错误
- 每个 step 一个 `ReplayStepView`
- summary 计数

每个 replay step 包含：

- `observation`
- `context`
- `resolved_terms`
- `retrieved_docs`
- `retrieved_skills`
- model repair/model action 事件
- 专表中的 `model_calls`
- parsed action
- action result
- runtime errors
- 简短 highlights
- raw events

## 前端

Dashboard 现在新增默认 `Replay` tab，位置在原始审计视图之前。

实现位置：

- `frontend/src/api/client.ts`
- `frontend/src/app/App.tsx`
- `frontend/src/shared/styles.css`

`Replay` tab 展示：

- 每个 step 一张卡片
- step status badge
- 简短 highlights
- 六个证据块：Observation、Context、Model、Action、Result、Errors
- 每个证据块都可以展开 JSON
- 每个 step 都可以展开 raw step events

原有 tab 仍然保留：

- `Timeline`
- `Model Calls`
- `Runtime Errors`

## 验证方式

聚焦验证：

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_dashboard_api.py
cd frontend && npm run typecheck
```

完整验证：

```bash
make ci
```

持久化 run 后手动检查 API：

```bash
curl http://127.0.0.1:8000/api/runs/<RUN_ID>/replay
```

手动检查 UI：

1. 启动 backend 和 frontend。
2. 运行 `scripts/demo_week3_agent.py --persist-db`。
3. 打开 `http://127.0.0.1:5173`。
4. 选中对应 run。
5. 打开 `Replay` tab。

## 设计说明

- replay API 是只读聚合视图。
- 它保留 raw events，因此 UI 不会替代审计源数据。
- 它只依赖 `run_id` 和 `step_index`，后续 Week 9 并行训练可以直接复用。
- skill 的 source run 也可以通过同一个 endpoint 回放，后续 Week 10 做 skill 质量治理时会用到。
