"""The execution engine: quota → policy → route → execute with fallback → price → telemetry."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from redis.exceptions import RedisError

from aura_billing.application.entitlements import EntitlementService, Reservation
from aura_billing.domain.entitlements import EntitlementSet
from aura_context import compile_documents, compress_history
from aura_engine.breaker import CircuitBreaker
from aura_engine.learning import LearnedChoice, LearnedSelector
from aura_engine.response_cache import CacheHit, ResponseCache
from aura_engine.telemetry import TelemetryPublisher
from aura_memory import LongTermMemory, SessionMemory
from aura_policy import CacheMode, ContextMode, ExecutionPolicy, QualityMode, merge
from aura_policy.pii import apply_pii_policy
from aura_providers.domain.catalog import ModelCatalog, ModelSpec, Tier
from aura_providers.domain.pricing import cost_usd
from aura_providers.domain.tokens import estimate_input_tokens
from aura_providers.domain.types import (
    ChatRequest,
    ChatResult,
    Message,
    ProviderError,
    ProviderErrorKind,
    StreamDone,
    TextDelta,
)
from aura_providers.registry import ProviderRegistry
from aura_routing import Route, Router
from aura_shared.errors import AuraError, DependencyUnavailable, QuotaExceeded, ValidationFailed
from aura_shared.events import ExecutionCompleted, ExecutionStatus, ModelCallRecord
from aura_shared.ids import new_id
from aura_shared.prompts import render

log = logging.getLogger("aura.engine")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    account_id: str
    workspace_id: str
    region: str
    priority: str
    plan_code: str | None = None
    environment_id: str | None = None
    api_key_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    model: str | None = None  # explicit model; None = AURA routes
    policy: ExecutionPolicy | None = None  # the request's own constraints (tightest layer)
    account_policy: ExecutionPolicy | None = None  # org/workload layers (resolved upstream)
    max_tokens: int | None = None
    mode: Literal["optimize", "passthrough"] = "optimize"
    trace_id: str | None = None
    session_id: str | None = None
    # Which persisted policy produced the account policy (for telemetry and canaries).
    policy_id: str | None = None
    policy_version_id: str | None = None
    deployment_id: str | None = None
    variant: str | None = None
    context_docs: tuple[str, ...] = ()  # /v1/execute context, compiled per policy
    memory_scopes: tuple[tuple[str, str], ...] = ()  # long-term memory / knowledge bases
    prompt_version_id: str | None = None  # registry prompt used for this request
    workload: str | None = None  # learning state is kept per workload


@dataclass(frozen=True, slots=True)
class EngineConfig:
    default_max_tokens: int = 16_000  # non-streaming default (keeps under HTTP timeouts)
    stream_default_max_tokens: int = 32_000
    max_attempts: int = 3
    timeout_s: float = 300.0
    session_token_budget: int = 8_000  # history injected for session_id requests
    default_context_budget: int = 16_000  # context_mode=auto without an explicit budget
    memory_token_budget: int = 1_500
    memory_top_k: int = 8
    base_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    model: str
    error: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class Execution:
    execution_id: str
    trace_id: str
    spec: ModelSpec
    result: ChatResult
    cost_usd: Decimal
    latency_ms: int
    route: Route
    attempts: tuple[AttemptRecord, ...]
    notes: tuple[str, ...]
    catalog_version: str
    credential_source: str = "platform"
    # What the same tokens would have cost on the priciest frontier model: the
    # counterfactual behind "How AURA optimized this" and shareable cards.
    baseline_cost_usd: Decimal | None = None

    @property
    def saved_usd(self) -> Decimal:
        if self.baseline_cost_usd is None:
            return Decimal(0)
        return max(Decimal(0), self.baseline_cost_usd - self.cost_usd)

    def aura_block(self) -> dict[str, Any]:
        d = self.route.decision
        return {
            "trace_id": self.trace_id,
            "execution_id": self.execution_id,
            "model": self.spec.id,
            "provider": self.spec.provider,
            "tier": self.spec.tier.name.lower(),
            "cost_usd": str(self.cost_usd),
            "latency_ms": self.latency_ms,
            "routing": {
                "router_version": d.router_version,
                "reason": d.reason,
                "candidates": d.candidates,
            },
            "attempts": [{"model": a.model, "error": a.error} for a in self.attempts],
            "notes": list(self.notes),
            "credential_source": self.credential_source,
            "catalog_version": self.catalog_version,
            "baseline_cost_usd": (
                str(self.baseline_cost_usd) if self.baseline_cost_usd is not None else None
            ),
            "saved_usd": str(self.saved_usd),
            "usage": {
                "input_tokens": self.result.usage.input_tokens,
                "output_tokens": self.result.usage.output_tokens,
            },
        }


@dataclass(frozen=True, slots=True)
class EngineDelta:
    text: str


@dataclass(frozen=True, slots=True)
class EngineDone:
    execution: Execution


@dataclass(frozen=True, slots=True)
class EngineError:
    error: AuraError
    execution_id: str
    trace_id: str


EngineEvent = EngineDelta | EngineDone | EngineError


@dataclass
class PreparedExecution:
    """The result of ``prepare``. Quota is reserved from here until the execution finishes."""

    ctx: ExecutionContext
    req: ChatRequest
    opts: ExecutionOptions
    route: Route
    policy: ExecutionPolicy
    entitlements: EntitlementSet
    execution_id: str
    trace_id: str
    started: float
    reservations: list[Reservation]
    notes: list[str] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    calls: list[ModelCallRecord] = field(default_factory=list)
    settled: bool = False
    credential_source: str = "platform"
    new_messages: tuple[Message, ...] = ()  # this turn's messages, stored on success
    cache_mode: CacheMode = CacheMode.OFF
    learning: LearnedChoice | None = None


_SKIP = object()
_REDIS_FAILURES = (RedisError, OSError, TimeoutError)
# When quota state is unreachable: business tiers keep running (an outage of our state
# store must not take down a customer's production); consumer tiers fail closed so an
# outage cannot become free unlimited usage.
FAIL_OPEN_PRIORITIES = frozenset({"b2b", "enterprise"})


def _to_aura_error(err: ProviderError) -> AuraError:
    if err.kind is ProviderErrorKind.BAD_REQUEST:
        return ValidationFailed(f"the model provider rejected the request ({err})")
    return DependencyUnavailable(f"model provider unavailable ({err.kind.value})")


class ExecutionObserver(Protocol):
    async def on_finished(
        self, run: PreparedExecution, execution: Execution | None, *, ok: bool
    ) -> None: ...


class CreditGate(Protocol):
    async def exhausted(self, account_id: str) -> bool: ...


class ExecutionEngine:
    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        router: Router,
        registry: ProviderRegistry,
        entitlements: EntitlementService,
        breaker: CircuitBreaker,
        publisher: TelemetryPublisher,
        config: EngineConfig | None = None,
        memory: SessionMemory | None = None,
        observers: list[ExecutionObserver] | None = None,
        response_cache: ResponseCache | None = None,
        long_memory: LongTermMemory | None = None,
        learner: LearnedSelector | None = None,
        credit_gate: CreditGate | None = None,
    ) -> None:
        self.catalog = catalog
        self._credit_gate = credit_gate
        self._router = router
        self._registry = registry
        self._ent = entitlements
        self._breaker = breaker
        self._publisher = publisher
        self._memory = memory
        self._observers = list(observers or [])
        self._cache = response_cache
        self._long_memory = long_memory
        self._learner = learner
        self.config = config or EngineConfig()

    # ---- prepare ---------------------------------------------------------------------
    async def prepare(
        self, ctx: ExecutionContext, req: ChatRequest, opts: ExecutionOptions, *, stream: bool
    ) -> PreparedExecution:
        """Reserve quota and route. Raises (QuotaExceeded, Forbidden, NoEligibleModel …)
        *before* any response bytes are sent, so HTTP can return a proper status."""
        execution_id, trace_id = new_id("exe"), opts.trace_id or new_id("trc")
        started = time.monotonic()
        if not req.messages:
            raise ValidationFailed("messages must not be empty")
        policy = merge(self.config.base_policy, opts.account_policy, opts.policy)
        if policy.effort and req.effort is None:
            req = dataclasses.replace(req, effort=policy.effort)
        default = (
            self.config.stream_default_max_tokens if stream else self.config.default_max_tokens
        )
        requested = opts.max_tokens or req.max_tokens or default
        if requested <= 0:
            raise ValidationFailed("max_tokens must be positive")
        ents = await self._ent.get(ctx.account_id)
        req, memory_notes = await self._inject_memory(ctx, req, opts, ents)
        req, context_notes = self._compile_context(req, opts, policy)
        context_notes = memory_notes + context_notes
        try:
            req, pii_notes = apply_pii_policy(req, policy.effective_pii)
        except AuraError as exc:
            self._emit_rejected(ctx, opts, execution_id, trace_id, started, exc)
            raise
        context_notes += pii_notes
        new_messages = tuple(m for m in req.messages if m.role != "system")
        notes: list[str] = []
        if opts.session_id and self._memory is not None:
            window = await self._memory.window(
                ctx.account_id, opts.session_id, token_budget=self.config.session_token_budget
            )
            if window.messages:
                system = tuple(m for m in req.messages if m.role == "system")
                req = dataclasses.replace(req, messages=system + window.messages + new_messages)
                notes.append(
                    f"session memory: {window.turns_included}/{window.turns_total} turns, "
                    f"~{window.est_tokens} tokens"
                )

        reservations: list[Reservation] = []
        degraded_notes: list[str] = []
        try:
            if ents.allows("executions"):
                try:
                    reservations.append(
                        await self._ent.reserve(
                            ctx.account_id,
                            "executions",
                            amount=1,
                            reservation_id=f"{execution_id}:executions",
                        )
                    )
                except _REDIS_FAILURES as exc:
                    if ctx.priority not in FAIL_OPEN_PRIORITIES:
                        log.error("quota store unavailable; failing closed", exc_info=True)
                        raise DependencyUnavailable("usage limits cannot be checked") from exc
                    log.error("quota store unavailable; failing open for %s", ctx.priority)
                    degraded_notes.append("quota check skipped: quota store unavailable")
            enabled = self.catalog.enabled()
            open_circuits = await self._breaker.open_circuits([m.id for m in enabled])
            usable = await self._usable_providers(ctx, degraded_notes)
            no_credentials = frozenset(m.id for m in enabled if m.provider not in usable)
            route = self._router.route(
                req,
                policy,
                est_input_tokens=estimate_input_tokens(req),
                requested_max_tokens=requested,
                explicit_model=opts.model,
                open_circuits=open_circuits,
                exclude=no_credentials,
                region=ctx.region,
            )
        except AuraError as exc:
            for r in reservations:
                await self._ent.release(r)
            self._emit_rejected(ctx, opts, execution_id, trace_id, started, exc)
            raise
        run = PreparedExecution(
            ctx, req, opts, route, policy, ents, execution_id, trace_id, started, reservations
        )
        run.notes.extend(context_notes + notes + degraded_notes)
        if (
            self._learner is not None
            and opts.model is None
            and policy.effective_quality is QualityMode.AUTO
            and len(route.chain) > 1
            and ents.allows("advanced_routing")
        ):
            try:
                run.route, run.learning = await self._learner.choose(
                    ctx.account_id, opts.workload, route
                )
            except _REDIS_FAILURES:
                log.warning("learned routing unavailable; using rules routing", exc_info=True)
        run.cache_mode = policy.effective_cache
        if run.cache_mode is CacheMode.SEMANTIC and not ents.allows("semantic_reuse"):
            run.cache_mode = CacheMode.EXACT
            run.notes.append("semantic cache not in plan: using exact cache only")
        run.new_messages = new_messages
        return run

    # ---- long-term memory / knowledge (RAG) --------------------------------------------
    async def _inject_memory(
        self, ctx: ExecutionContext, req: ChatRequest, opts: ExecutionOptions, ents: EntitlementSet
    ) -> tuple[ChatRequest, list[str]]:
        if not opts.memory_scopes or self._long_memory is None:
            return req, []
        if not (ents.allows("knowledge_retrieval") or ents.allows("personal_memory")):
            if any(scope != "user" for scope, _ in opts.memory_scopes):
                return req, ["memory/knowledge retrieval not in plan: skipped"]
            return req, []  # personal memory is silently off on plans without it
        query = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        try:
            hits = await self._long_memory.search(
                ctx.account_id,
                scopes=list(opts.memory_scopes),
                query=query,
                k=self.config.memory_top_k,
            )
        except Exception:
            log.warning("memory retrieval failed", exc_info=True)
            return req, ["memory retrieval unavailable"]
        if not hits:
            return req, ["memory: no relevant items"]
        lines, used = [], 0
        for h in hits:
            t = estimate_input_tokens(ChatRequest((Message("user", h.content),)))
            if used + t > self.config.memory_token_budget:
                break
            lines.append(f"- [{h.scope}:{h.scope_ref}] {h.content}")
            used += t
        block = Message("system", render("memory/memory_block.j2", lines=lines))
        system = tuple(m for m in req.messages if m.role == "system")
        rest = tuple(m for m in req.messages if m.role != "system")
        return dataclasses.replace(req, messages=(*system, block, *rest)), [
            f"memory: {len(lines)} items injected (~{used} tokens)"
        ]

    # ---- context -----------------------------------------------------------------------
    def _compile_context(
        self, req: ChatRequest, opts: ExecutionOptions, policy: ExecutionPolicy
    ) -> tuple[ChatRequest, list[str]]:
        """Attach /v1/execute context and fit everything to the context budget per the
        policy's context mode. STRICT never alters what the customer sent."""
        mode = policy.effective_context
        budget = policy.context_token_budget or self.config.default_context_budget
        notes: list[str] = []
        msgs = list(req.messages)
        if opts.context_docs:
            query = next((m.content for m in reversed(msgs) if m.role == "user"), "")
            if mode is ContextMode.STRICT:
                text = "\n\n".join(opts.context_docs)
            else:
                compiled = compile_documents(opts.context_docs, query, token_budget=budget)
                text = compiled.text
                notes.append(compiled.summary())
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].role == "user":
                    msgs[i] = Message("user", f"{msgs[i].content}\n\n<context>\n{text}\n</context>")
                    break
        if mode is not ContextMode.STRICT:
            msgs, note = compress_history(msgs, token_budget=budget)
            if note:
                notes.append(note)
        return dataclasses.replace(req, messages=tuple(msgs)), notes

    # ---- helpers ---------------------------------------------------------------------
    def _remaining(self, run: PreparedExecution) -> float:
        budget = self.config.timeout_s
        if run.policy.max_latency_ms is not None:
            budget = min(budget, run.policy.max_latency_ms / 1000)
        return budget - (time.monotonic() - run.started)

    async def _premium_guard(self, run: PreparedExecution, spec: ModelSpec) -> Any:
        """Frontier calls consume `premium_model_calls` where the plan meters it."""
        if spec.tier is not Tier.FRONTIER or not run.entitlements.allows("premium_model_calls"):
            return None
        try:
            return await self._ent.reserve(
                run.ctx.account_id,
                "premium_model_calls",
                amount=1,
                reservation_id=f"{run.execution_id}:premium:{spec.id}",
            )
        except QuotaExceeded:
            run.notes.append(f"premium model quota exhausted: skipped {spec.id}")
            return _SKIP

    async def _record_failure(
        self, run: PreparedExecution, spec: ModelSpec, err: ProviderError, t0: float
    ) -> None:
        latency = int((time.monotonic() - t0) * 1000)
        run.attempts.append(AttemptRecord(spec.id, err.kind.value, latency))
        run.calls.append(
            ModelCallRecord(
                provider=spec.provider,
                model=spec.id,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency,
                cost=Decimal(0),
                error=err.kind.value,
                credential_source=run.credential_source,
            )
        )
        if err.counts_against_health:
            await self._breaker.record_failure(spec.id)

    def _served_spec(self, attempted: ModelSpec, result: ChatResult, notes: list[str]) -> ModelSpec:
        for spec in self.catalog.models.values():
            if spec.provider == attempted.provider and spec.upstream == result.model_served:
                return spec
        notes.append(
            f"served by {result.model_served} (not in catalog); priced at {attempted.id} rates"
        )
        return attempted

    async def _finish(
        self, run: PreparedExecution, attempted: ModelSpec, result: ChatResult, t0: float
    ) -> Execution:
        notes = [*run.notes, *result.notes]
        spec = self._served_spec(attempted, result, notes)
        cost = cost_usd(spec, result.usage)
        latency = int((time.monotonic() - run.started) * 1000)
        call_latency = int((time.monotonic() - t0) * 1000)
        run.attempts.append(AttemptRecord(attempted.id, None, call_latency))
        run.calls.append(
            ModelCallRecord(
                provider=spec.provider,
                model=spec.id,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                latency_ms=call_latency,
                cost=cost,
                credential_source=run.credential_source,
            )
        )
        for r in run.reservations:
            try:
                await self._ent.commit(r, actual=1)
            except _REDIS_FAILURES:  # reservation lease expiry + reconciler converge
                log.warning("quota commit failed", exc_info=True)
        run.settled = True
        if run.opts.session_id and self._memory is not None and result.text:
            try:
                await self._memory.append(
                    run.ctx.account_id,
                    run.opts.session_id,
                    [*run.new_messages, Message("assistant", result.text)],
                )
            except Exception:
                log.warning("session memory append failed", exc_info=True)
        if run.cache_mode is not CacheMode.OFF and self._cache is not None:
            await self._cache.store(
                run.ctx.account_id,
                attempted,
                run.req,
                result,
                cost,
                max_tokens=run.route.max_tokens[attempted.id],
                ttl_s=run.policy.cache_ttl_s,
                semantic=run.cache_mode is CacheMode.SEMANTIC,
            )
        execution = Execution(
            execution_id=run.execution_id,
            trace_id=run.trace_id,
            spec=spec,
            result=result,
            cost_usd=cost,
            latency_ms=latency,
            route=run.route,
            attempts=tuple(run.attempts),
            notes=tuple(notes),
            catalog_version=self.catalog.version,
            credential_source=run.credential_source,
            baseline_cost_usd=self._baseline_cost(result),
        )
        self._emit(run, ExecutionStatus.SUCCEEDED, cost, latency)
        await self._notify(run, execution, ok=True)
        return execution

    async def _abandon(self, run: PreparedExecution, reason: str) -> None:
        """Release quota and emit a FAILED event (idempotent). ``reason`` is a code."""
        if run.settled:
            return
        run.settled = True
        for r in run.reservations:
            try:
                await self._ent.release(r)
            except _REDIS_FAILURES:  # the reservation lease expires on its own
                log.warning("quota release failed", exc_info=True)
        latency = int((time.monotonic() - run.started) * 1000)
        self._emit(run, ExecutionStatus.FAILED, Decimal(0), latency, error_code=reason)
        await self._notify(run, None, ok=False)

    async def _cache_lookup(self, run: PreparedExecution) -> CacheHit | None:
        if run.cache_mode is CacheMode.OFF or self._cache is None:
            return None
        spec = run.route.primary
        return await self._cache.lookup(
            run.ctx.account_id,
            spec,
            run.req,
            max_tokens=run.route.max_tokens[spec.id],
            semantic=run.cache_mode is CacheMode.SEMANTIC,
            similarity=run.policy.cache_similarity,
        )

    async def _finish_cached(self, run: PreparedExecution, hit: CacheHit) -> Execution:
        spec = run.route.primary
        for r in run.reservations:  # a cached answer is still an execution served
            try:
                await self._ent.commit(r, actual=1)
            except _REDIS_FAILURES:
                log.warning("quota commit failed", exc_info=True)
        run.settled = True
        if run.opts.session_id and self._memory is not None:
            try:
                await self._memory.append(
                    run.ctx.account_id,
                    run.opts.session_id,
                    [*run.new_messages, Message("assistant", hit.result.text)],
                )
            except Exception:
                log.warning("session memory append failed", exc_info=True)
        latency = int((time.monotonic() - run.started) * 1000)
        note = f"served from {hit.kind} cache (saved ${hit.saved_cost})"
        if hit.similarity is not None:
            note += f", similarity {hit.similarity}"
        run.attempts.append(AttemptRecord(spec.id, None, 0))
        execution = Execution(
            execution_id=run.execution_id,
            trace_id=run.trace_id,
            spec=spec,
            result=hit.result,
            cost_usd=Decimal(0),
            latency_ms=latency,
            route=run.route,
            attempts=tuple(run.attempts),
            notes=(*run.notes, note),
            catalog_version=self.catalog.version,
            credential_source="cache",
            baseline_cost_usd=self._baseline_cost(hit.result),
        )
        self._emit(
            run,
            ExecutionStatus.CACHED,
            Decimal(0),
            latency,
            cache_hit=hit.kind,
            cost_saved=hit.saved_cost,
        )
        await self._notify(run, execution, ok=True)
        return execution

    def _baseline_cost(self, result: ChatResult) -> Decimal | None:
        frontier = [m for m in self.catalog.enabled() if m.tier is Tier.FRONTIER]
        if not frontier:
            return None
        return max(cost_usd(m, result.usage) for m in frontier)

    async def _notify(
        self, run: PreparedExecution, execution: Execution | None, *, ok: bool
    ) -> None:
        for observer in self._observers:
            try:
                await observer.on_finished(run, execution, ok=ok)
            except Exception:
                log.warning("execution observer failed", exc_info=True)

    async def usable_providers(self, account_id: str) -> set[str]:
        """Providers this account can call now (for model pickers)."""
        return await self._registry.usable_providers(account_id)

    async def _usable_providers(self, ctx: ExecutionContext, notes: list[str]) -> set[str]:
        """Consumers out of credits may still run on their own keys (BYOK)."""
        if self._credit_gate is None or not await self._credit_gate.exhausted(ctx.account_id):
            return await self._registry.usable_providers(ctx.account_id)
        sources = await self._registry.credential_sources(ctx.account_id)
        own = {name for name, source in sources.items() if source != "platform"}
        if not own:
            raise QuotaExceeded(
                "out of credits: buy a credit pack, upgrade, or add your own provider key"
            )
        notes.append("credits exhausted: using your own provider keys only")
        return own

    # ---- telemetry -------------------------------------------------------------------
    def _emit(
        self,
        run: PreparedExecution,
        status: ExecutionStatus,
        cost: Decimal,
        latency: int,
        *,
        error_code: str | None = None,
        cache_hit: str | None = None,
        cost_saved: Decimal | None = None,
    ) -> None:
        try:
            self._publisher.publish(
                ExecutionCompleted(
                    account_id=run.ctx.account_id,
                    region=run.ctx.region,
                    trace_id=run.trace_id,
                    execution_id=run.execution_id,
                    workspace_id=run.ctx.workspace_id,
                    environment_id=run.ctx.environment_id,
                    session_id=run.opts.session_id,
                    mode=run.opts.mode,
                    status=status,
                    total_latency_ms=latency,
                    total_cost=cost,
                    calls=list(run.calls),
                    routing=run.route.decision,
                    error_code=error_code,
                    notes=list(run.notes),
                    policy_id=run.opts.policy_id,
                    policy_version_id=run.opts.policy_version_id,
                    deployment_id=run.opts.deployment_id,
                    variant=run.opts.variant,
                    prompt_version_id=run.opts.prompt_version_id,
                    cache_hit=cache_hit,
                    cost_saved=cost_saved,
                )
            )
        except Exception:
            log.exception("failed to publish execution telemetry")

    def _emit_rejected(
        self,
        ctx: ExecutionContext,
        opts: ExecutionOptions,
        execution_id: str,
        trace_id: str,
        started: float,
        exc: AuraError,
    ) -> None:
        try:
            self._publisher.publish(
                ExecutionCompleted(
                    account_id=ctx.account_id,
                    region=ctx.region,
                    trace_id=trace_id,
                    execution_id=execution_id,
                    workspace_id=ctx.workspace_id,
                    environment_id=ctx.environment_id,
                    mode=opts.mode,
                    status=ExecutionStatus.REJECTED,
                    total_latency_ms=int((time.monotonic() - started) * 1000),
                    total_cost=Decimal(0),
                    session_id=opts.session_id,
                    error_code=exc.code,
                )
            )
        except Exception:
            log.exception("failed to publish rejection telemetry")

    # ---- non-streaming ---------------------------------------------------------------
    async def execute(self, run: PreparedExecution) -> Execution:
        try:
            if (hit := await self._cache_lookup(run)) is not None:
                return await self._finish_cached(run, hit)
            last_error: AuraError | None = None
            for spec in run.route.chain[: self.config.max_attempts]:
                premium = await self._premium_guard(run, spec)
                if premium is _SKIP:
                    last_error = QuotaExceeded("premium model quota exhausted")
                    continue
                remaining = self._remaining(run)
                if remaining <= 0:
                    last_error = DependencyUnavailable("execution deadline exceeded")
                    break
                t0 = time.monotonic()
                try:
                    provider = self._registry.provider_for(spec)
                    creds = await self._registry.credentials_for(run.ctx.account_id, spec)
                    run.credential_source = creds.source
                    async with asyncio.timeout(remaining):
                        result = await provider.complete(
                            spec, run.req, creds, max_tokens=run.route.max_tokens[spec.id]
                        )
                except (ProviderError, TimeoutError) as exc:
                    err = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError(ProviderErrorKind.TIMEOUT, "deadline exceeded")
                    )
                    if premium is not None:
                        await self._ent.release(premium)
                    await self._record_failure(run, spec, err, t0)
                    if not err.retryable:
                        raise _to_aura_error(err) from exc
                    last_error = _to_aura_error(err)
                    continue
                if premium is not None:
                    await self._ent.commit(premium, actual=1)
                return await self._finish(run, spec, result, t0)
            raise last_error or DependencyUnavailable("no model could serve the request")
        except BaseException:
            await self._abandon(run, "execution_failed")
            raise

    # ---- streaming -------------------------------------------------------------------
    async def stream(self, run: PreparedExecution) -> AsyncGenerator[EngineEvent, None]:
        """Falls back across models until the first token; afterwards the model is fixed."""
        try:
            if (hit := await self._cache_lookup(run)) is not None:
                yield EngineDelta(hit.result.text)
                yield EngineDone(await self._finish_cached(run, hit))
                return
            last_error: AuraError | None = None
            for spec in run.route.chain[: self.config.max_attempts]:
                premium = await self._premium_guard(run, spec)
                if premium is _SKIP:
                    last_error = QuotaExceeded("premium model quota exhausted")
                    continue
                remaining = self._remaining(run)
                if remaining <= 0:
                    last_error = DependencyUnavailable("execution deadline exceeded")
                    break
                t0 = time.monotonic()
                try:
                    provider = self._registry.provider_for(spec)
                    creds = await self._registry.credentials_for(run.ctx.account_id, spec)
                    run.credential_source = creds.source
                except ProviderError as exc:
                    await self._record_failure(run, spec, exc, t0)
                    if premium is not None:
                        await self._ent.release(premium)
                    last_error = _to_aura_error(exc)
                    if exc.retryable:
                        continue
                    break
                agen = provider.stream(
                    spec, run.req, creds, max_tokens=run.route.max_tokens[spec.id]
                )
                try:
                    async with asyncio.timeout(remaining):
                        event = await anext(agen)
                except (ProviderError, TimeoutError, StopAsyncIteration) as exc:
                    await agen.aclose()
                    err = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError(ProviderErrorKind.TIMEOUT, "no first token in time")
                        if isinstance(exc, TimeoutError)
                        else ProviderError(ProviderErrorKind.UNAVAILABLE, "empty stream")
                    )
                    if premium is not None:
                        await self._ent.release(premium)
                    await self._record_failure(run, spec, err, t0)
                    last_error = _to_aura_error(err)
                    if err.retryable:
                        continue
                    break
                # Committed to this model: tokens are flowing to the client.
                try:
                    while True:
                        if isinstance(event, TextDelta):
                            yield EngineDelta(event.text)
                        elif isinstance(event, StreamDone):
                            if premium is not None:
                                await self._ent.commit(premium, actual=1)
                            yield EngineDone(await self._finish(run, spec, event.result, t0))
                            return
                        async with asyncio.timeout(max(self._remaining(run), 0.001)):
                            event = await anext(agen)
                except (ProviderError, TimeoutError, StopAsyncIteration) as exc:
                    err = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError(ProviderErrorKind.TIMEOUT, "stream stalled")
                    )
                    if premium is not None:
                        await self._ent.release(premium)
                    await self._record_failure(run, spec, err, t0)
                    await self._abandon(run, "stream_interrupted")
                    yield EngineError(
                        DependencyUnavailable("the model stream was interrupted"),
                        run.execution_id,
                        run.trace_id,
                    )
                    return
                finally:
                    await agen.aclose()
            await self._abandon(run, "no_model_available")
            yield EngineError(
                last_error or DependencyUnavailable("no model could serve the request"),
                run.execution_id,
                run.trace_id,
            )
        finally:
            # Client disconnects cancel the generator; never leak a quota reservation.
            await self._abandon(run, "stream_closed_early")
