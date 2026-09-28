# Determinism audit backlog — 2026-09-28

Created in the session-handoff-kit AgentsRoom project. All eleven fixes are implemented and validated for v0.12.0 as of 2026-09-28; every ticket is marked done with per-topic evidence. Each ticket retains its original problem and acceptance checks.

| Topic | AgentsRoom task ID |
| --- | --- |
| Return explicit lookup outcomes and next actions | `886ba881-f81a-4953-95d9-9f84196328ab` |
| Handle handoff write and permission failures deterministically | `02359aed-120b-4461-b03c-776460610564` |
| Enforce ownership for every handoff state mutation | `a4f9197d-e456-41a1-b70b-343ebfeaf5ab` |
| Unify authoritative topic state across list and resolve | `853ff501-1bc2-4e4a-9d2b-fc774dd39982` |
| Save and supersede handoffs through one transactional command | `5ef9ee8c-59ff-4708-90ad-ea23b58ae21a` |
| Unify startup and skill resume authorization policy | `24be2bb9-64a8-42f4-bb3c-629eb740e75f` |
| Keep resume preparation recoverable until transfer is acknowledged | `6f0f853d-0fcc-4396-997c-09c231ad2e37` |
| Identify completed checkpoints by session and trigger | `9c205b19-b201-4c51-939f-4df1941f951d` |
| Scope handoff session facts to the actual session | `274f66bc-2b8c-474f-813d-1d28cca24c2d` |
| Track explicit handoff lineage instead of inferring from branch | `5aeda3ee-81f1-4d12-91bc-cc4db39f5772` |
| Route explicit resume requests without a model call | `3046bfe6-3b05-4cef-b2ab-4c90b3a4d1f9` |

Follow-up: [context-delivery evaluation and implementation results](handoff-context-evaluation-2026-09-28.md). Context findings C1–C7 are also implemented; only the eleven preceding determinism topics were filed as separate backlog tickets.
