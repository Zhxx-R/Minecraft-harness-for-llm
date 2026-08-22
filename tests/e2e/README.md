# End-to-End Tests

E2E scenarios will validate the full harness path:

1. Start backend, worker, database, Redis, and Minecraft server.
2. Import a small task manifest.
3. Run the agent with `qwen3.7-plus`.
4. Verify trajectory logs, runtime recovery, and skill candidate behavior.

