# aura-memory

**Responsibility:** memory that context assembly draws on (ADR-0004).

V1 provides:
- `SessionMemory`: recent conversation turns per `(account, session_id)`, held in the Redis *state* role.
  - Turns are capped by count, and reads are packed newest-first into a token budget.
  - The TTL slides 24 h from last use.
  - An optional rolling summary can be stored for the turns that fall out of the window. The summarizer worker that writes it arrives with the V2 context compiler.
- `WorkingMemory`: run-scoped scratch space (a Redis hash per `run_id`) for the V3 graph runtime.

Every key carries the tenant hash tag, so one tenant can never address another tenant's session.

**Later:** long-term episodic/semantic memory, hybrid retrieval, consolidation (V2), procedural memory (V4), consumer memory UI (V5).
