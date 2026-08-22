# Week 1 开发文档

## 目标

Week 1 建立工程基线和最小可用 Minecraft 知识层。Agent 不应依赖模型预训练记忆去猜 `wooden pickaxe`、`log`、`plank`、`crafting table` 等 Minecraft 专有名词。

## 已交付变更

- 将完整 16 周计划保存到 `docs/plans/16-week-development-plan.md`。
- 新增 CI workflow：`.github/workflows/ci.yml`。
  - Python backend 安装、编译、schema 校验、测试。
  - Mineflayer worker TypeScript typecheck。
  - Frontend TypeScript typecheck。
- 新增 `scripts/validate_json_schemas.py`，用于 shared schema 校验。
- 新增 `Makefile` targets：
  - `make validate-schemas`
  - `make test-python`
  - `make ci`
- 新增最小 Minecraft 知识数据：`knowledge/raw/minimal_minecraft_knowledge.json`。
- 新增 Mineflayer 操作说明：`knowledge/raw/mineflayer_operation_guide.md`。
- 新增 backend knowledge package：
  - `KnowledgeProvider` protocol。
  - `StaticKnowledgeProvider` 确定性的文件型实现。
  - terms、recipes、documents、resolved terms 的 dataclass models。
- 新增术语解析、recipe lookup、本地文档检索单测。

## 知识库设计

Week 1 的 provider 是确定性、小规模实现，不是向量数据库。即使 Week 4 接入 SQL 持久化后，它仍然有价值，因为 canonical ID 和 recipe 应该通过确定性 lookup 解析，而不是依赖 embedding retrieval 猜测。

`StaticKnowledgeProvider.resolve_terms(task_text)`：

- 扫描 task text 中的已知 aliases。
- 返回 canonical Minecraft IDs。
- 如果目标可合成，附带 recipe hints。

`StaticKnowledgeProvider.get_recipe(item_id)`：

- 返回 station、ingredients、output count、required station/block 和 description。

`StaticKnowledgeProvider.retrieve_docs(query, limit)`：

- 使用本地 guide documents 的简单 lexical overlap。
- 只返回项目内本地知识，不使用 web search。

## Week 1 验收示例

输入：

```text
Craft a wooden pickaxe from logs, planks, and a crafting table.
```

期望解析结果：

- `wooden_pickaxe`：item，recipe 需要 `crafting_table`，ingredients 为 `oak_planks x3`、`stick x2`。
- `oak_log`：block，早期树木资源。
- `oak_planks`：item，由 `oak_log` 合成。
- `crafting_table`：block/station，由 `oak_planks x4` 合成。

该行为由 `backend/tests/unit/test_knowledge_provider.py` 覆盖。

## 开发命令

安装 backend dev dependencies：

```bash
python -m pip install -e "backend[dev]"
```

运行 backend tests：

```bash
cd backend
python -m pytest
```

校验 shared JSON schemas：

```bash
python scripts/validate_json_schemas.py
```

安装 Node 依赖后运行本地 CI：

```bash
make ci
```

## 当前边界

- 知识层是 local-only、deterministic。
- 不使用 web search。
- Week 1 不修改 Mineflayer runtime 行为。
- Provider 尚未接入 `ContextManager`；这是 Week 3 在 worker RPC 和 model loop 可用后的工作。
- SQL-backed 知识持久化从 Week 4 开始。pgvector 在 Week 4 作为后续 vector/hybrid retrieval 的基础能力启用，但确定性的 term 和 recipe lookup 仍然是 harness contract 的一部分。

## 验证结果

Week 1 完成条件：

```bash
python -m compileall -q backend/src
python scripts/validate_json_schemas.py
cd backend && python -m pytest
```

Worker 和 frontend typechecks 已配置到 CI；本地运行前需要分别在两个 Node 项目中执行 `npm install`。

本地已用以下命令验证：

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make ci
```

结果：

- Shared schema validation passed：3 个 schemas。
- Backend tests passed：4 个 tests。
- Worker TypeScript typecheck passed。
- Frontend TypeScript typecheck passed。

已知非阻塞 warning：

- FastAPI/Starlette 的上游包会输出 `TestClient` deprecation warning。
- `npm install` 在 frontend 和 worker dependency tree 中报告 audit warnings。暂不使用 `npm audit fix --force`，因为强制升级可能在 runtime 集成前引入 breaking changes。
