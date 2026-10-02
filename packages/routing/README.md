# aura-routing

**Responsibility:** pick the model (and fallback chain) for a request under its policy.

`rules-v1` works in five steps:
1. **Hard constraints:** policy allow/deny lists, data region, context-window fit, the per-request cost cap (which also clamps `max_tokens` to what the budget affords), and open circuit breakers.
2. **Target tier:** from the quality mode (`fast | balanced | best`), or in `auto` mode from a transparent complexity score over task features. The features are reasoning cues, code, math, length, turn count and output size.
3. **Primary model:** the cheapest eligible model in the target tier. If the tier is empty, it takes the nearest tier, preferring a higher tier over a lower one.
4. **Fallback chain:** the next-best eligible models. With frontier set to `escalation_only`, frontier models appear only here, never as primary.
5. **Logging:** every decision records its features, candidates and propensity. The learned router (V4) trains on this off-policy data.

**Does not do:** call models (`aura-engine`) or learn (V4).
