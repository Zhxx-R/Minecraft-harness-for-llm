# Architecture

The harness follows the six-component shape `H=(E,T,C,S,L,V)`.

- `E` Execution loop: observe, think, act, verify, reflect, checkpoint, recover.
- `T` Tool registry: validated runtime, knowledge, and skill tool interfaces.
- `C` Context manager: prompt assembly, state compression, previous tool results, and visual frame policy. It should not decide which knowledge to retrieve by default.
- `S` State store: run state, task memory, skill versions, checkpoints.
- `L` Lifecycle hooks: policy, audit, authentication, quotas, failure gates.
- `V` Evaluation interface: typed trajectories, verifier results, costs, and replay data.

Mineflayer is the online execution runtime. MineDojo supplies task definitions and evaluation metadata. Knowledge access is exposed as read-only harness tools selected by the model and audited by the harness.
