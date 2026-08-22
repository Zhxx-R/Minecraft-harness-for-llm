# ADR 0001: Runtime Boundaries

## Decision

The agent will not see MineDojo and Mineflayer as two peer execution APIs.

Mineflayer is the only online game runtime. MineDojo is a task and evaluation provider. The harness exposes a single high-level action schema to the model.

## Rationale

Exposing both APIs would encourage mixed Python/JavaScript plans and unstable code generation. The harness should constrain interaction into auditable, validated, replayable actions and skills.

## Consequences

- The Mineflayer worker owns bot execution details.
- MineDojo task metadata is converted into harness task manifests.
- Skill code, when needed, must be generated and validated outside the primary agent loop.

