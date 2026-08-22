# Knowledge Sources

Place raw offline knowledge sources in `knowledge/raw/`.

Expected MVP sources:

- MineDojo wiki/task documentation exports.
- Mineflayer API and operation guide.
- Project-authored Minecraft operation notes.

Generated chunks live in `knowledge/processed/`; vector indexes or metadata indexes live in `knowledge/indexes/`.
The deterministic Minecraft 1.20.1 snapshot at `knowledge/processed/minecraft_1_20_1_knowledge.json`
is checked in because it is part of the offline harness contract.

## Week 1 Minimal Knowledge

`knowledge/raw/minimal_minecraft_knowledge.json` is the deterministic bootstrap knowledge base used before pgvector/RAG is introduced.

It includes:

- Minecraft glossary entries.
- Canonical item/block/entity IDs.
- Starter recipes for wood, planks, sticks, crafting table, wooden pickaxe, and furnace.
- Local guide snippets for Mineflayer action usage and early-game crafting chains.

The backend loads this file through `StaticKnowledgeProvider`.

## Minecraft 1.20.1 Generated Knowledge

`knowledge/processed/minecraft_1_20_1_knowledge.json` is generated from the
`minecraft-data` package bundled with the Mineflayer worker. It supplements the
minimal bootstrap file with:

- canonical item, block, and entity terms;
- aliases based on display names and canonical IDs;
- representative crafting recipes for 729 craftable outputs.

Rebuild it after changing the target Minecraft version or worker dependency:

```bash
make build-knowledge
```

The provider loads the minimal hand-authored file first and then the generated
snapshot. Hand-authored entries intentionally override generated entries when an
ID appears in both sources.
