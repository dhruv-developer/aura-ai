# aura-policy

**Responsibility:** the `ExecutionPolicy` object (spec §61, §139) and how layers combine.

Layers are merged in this order: global → organization → project → workload → user preference → per-request constraints.
- **Hard limits merge stricter-wins:** cost and latency caps take the minimum, allowed models intersect, disallowed models are unioned, frontier usage takes the strictest value, and data regions intersect. A request can tighten a limit but never loosen one.
- **Preferences merge last-wins:** for quality mode and effort, the most specific layer that sets a value wins.

V1 covers the fields the runtime enforces today. Prompt, context, retrieval, cache, graph and verification sections arrive with the V2/V3 optimizers. Persisted, versioned policies and bindings arrive with the policy engine (V2).
