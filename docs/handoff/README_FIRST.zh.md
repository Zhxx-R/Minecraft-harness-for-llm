# Minecraft Agent Harness 安装包：先读这里

这个目录应包含：

- `minecraft-agent-harness-handoff-*.tar.gz`：推荐给 macOS/Linux/WSL2 使用。
- `minecraft-agent-harness-handoff-*.tar.gz.sha256`：压缩包校验和。
- `minecraft-agent-harness-handoff-*.zip`：备用 ZIP 包。
- `install_handoff.sh`：校验、解压并执行 bootstrap。

## 交给 Codex 的提示词

可以把下面这段原样发给接收方的 Codex：

```text
请安装这个 Minecraft Agent Harness 压缩包。先校验 SHA-256，再解压到一个新的空目录；阅读 CODEX_HANDOFF.md 和 docs/handoff/installation.zh.md；运行环境检查和 offline demo。不要读取或输出任何旧凭据，不要让我在聊天中粘贴 API Key。缺少系统依赖时先告诉我准确的安装命令。只有在我明确确认接受 Minecraft EULA 后，才安装并启动 Minecraft server。配置好我自己的 QWEN_API_KEY 后，再运行 harvest_1_dirt 的 live demo，并启动审计前端展示该 run。
```

## 自动解压与安装

```bash
bash install_handoff.sh \
  minecraft-agent-harness-handoff-<timestamp>.tar.gz \
  "$HOME/minecraft-agent-harness" \
  --skip-tests
```

去掉 `--skip-tests` 会在安装后执行完整 CI，推荐正式验收时执行。

完整说明见压缩包内的 `docs/handoff/installation.zh.md`。
