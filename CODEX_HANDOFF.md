# Codex Handoff Protocol

This file is the entry point for an agent configuring a freshly extracted package.

## Objective

Prepare the repository, run a deterministic offline smoke test, then run one real Minecraft LLM task when the user has supplied their own Qwen-compatible API key and explicitly accepted the Minecraft server EULA.

## Safety Boundaries

- Never print, commit, archive, or paste `.env` values into chat.
- Never reuse credentials found in old logs or shell history.
- Do not expose the generated Minecraft server outside `127.0.0.1` without a separate security review.
- Do not record EULA acceptance unless the recipient explicitly confirms it.
- Do not install system packages silently. Report missing prerequisites and use the recipient's preferred package manager.

## Automated Sequence

From the extracted repository root:

```bash
./scripts/handoff/bootstrap.sh --check-only
./scripts/handoff/autorun.sh offline
```

The first command reports missing prerequisites. The second installs project dependencies, runs CI, and writes an offline demo under `runs/handoff_demo_<UTC timestamp>/`.

For a live demo:

1. Ask the user to set `QWEN_API_KEY` in `.env` or export it in their own shell. Do not ask them to paste it into the conversation.
2. Ask the user to read and explicitly accept <https://aka.ms/MinecraftEULA>.
3. Run:

```bash
./scripts/handoff/autorun.sh live --accept-minecraft-eula --task-id harvest_1_dirt
```

The live sequence downloads verified Minecraft/Fabric/Carpet assets, starts a loopback-only server, verifies the model endpoint, runs one real Mineflayer task, and saves JSON plus SQLite audit artifacts.

After the live run, start the audit UI using the exact database path printed by the demo:

```bash
./scripts/handoff/start_dashboard.sh --database-path runs/handoff_demo_<timestamp>/live_demo.sqlite3
```

Open `http://127.0.0.1:5173`. Stop managed processes with:

```bash
./scripts/handoff/stop_services.sh --minecraft --docker
```

## Success Evidence

Do not report success from process exit alone. Verify:

- `make ci` passed.
- `tasks/executable/minedojo_programmatic_tasks.summary.json` reports the expected executable/unsupported counts.
- Offline demo contains `prompt.json` and benchmark reports.
- Live demo contains `model_verification.json`, `live_demo.json`, `live_demo.sqlite3`, and `live_demo.log`.
- The SQLite `runs` record has a terminal status and trajectory/model-call rows are queryable.

See `docs/handoff/installation.zh.md` for troubleshooting and full manual commands.
