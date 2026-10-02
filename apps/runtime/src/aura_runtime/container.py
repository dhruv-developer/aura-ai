"""Runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from aura_auth.application.api_keys import ApiKeyService
from aura_auth.application.identity import IdentityService
from aura_auth.application.sessions import SessionStore
from aura_billing.application.consumer import CreditGate, CreditMeter
from aura_billing.application.credits import CreditService
from aura_billing.application.entitlements import EntitlementService
from aura_billing.domain.catalog import Catalog as BillingCatalog
from aura_billing.domain.catalog import load_catalog as load_billing_catalog
from aura_cache import InvalidationBus, L1Cache, RedisL2, TieredCache
from aura_credentials import ProviderCredentialService
from aura_db import Database
from aura_deploy import OutcomeService, PolicyResolver, VariantStats
from aura_engine import (
    CircuitBreaker,
    EngineConfig,
    ExecutionEngine,
    InMemoryPublisher,
    TelemetryPublisher,
)
from aura_engine.learning import LearnedSelector
from aura_engine.response_cache import ResponseCache
from aura_engine.sharing import ExecutionSummaries, SummaryObserver
from aura_engine.telemetry import NatsPublisher
from aura_engine.wiring import build_provider_stack
from aura_graph.agents import AgentLimits, AgentOrchestrator
from aura_graph.service import GraphRunService
from aura_graph.wiring import build_executor
from aura_memory import ConversationStore, LongTermMemory, SessionMemory, WorkingMemory
from aura_prompts import PromptRegistry
from aura_providers.adapters.stub import StubProvider
from aura_providers.domain.types import Credentials
from aura_providers.embeddings import Embedder, HashingEmbedder, OpenAICompatibleEmbedder
from aura_providers.ports import ModelProvider
from aura_providers.registry import (
    CredentialResolver,
)
from aura_routing import Router
from aura_runtime.observers import CreditObserver, DeployObserver, LearningObserver
from aura_runtime.session_auth import SessionAccessResolver
from aura_shared.redis import KeyFamily, RedisPools, RedisRole, RedisScripts
from aura_shared.settings import Settings

# Stub credentials never leave the process; they only satisfy the registry contract.
STUB_CREDENTIALS = Credentials(api_key="stub", source="platform")


def _embedder(settings: Settings, http: httpx.AsyncClient) -> Embedder:
    if settings.embedding_backend == "openai" and settings.openai_api_key:
        return OpenAICompatibleEmbedder(
            http,
            base_url=settings.embedding_base_url,
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )
    return HashingEmbedder()


@dataclass
class RuntimeContainer:
    settings: Settings
    db: Database
    redis: RedisPools
    scripts: RedisScripts
    http: httpx.AsyncClient
    api_keys: ApiKeyService
    entitlements: EntitlementService
    billing_catalog: BillingCatalog
    engine: ExecutionEngine
    publisher: TelemetryPublisher
    stub: StubProvider | None
    invalidation: InvalidationBus
    credentials: ProviderCredentialService
    policies: PolicyResolver
    outcomes: OutcomeService
    long_memory: LongTermMemory
    prompts: PromptRegistry
    graphs: GraphRunService
    learner: LearnedSelector
    credit_gate: CreditGate
    credit_meter: CreditMeter
    sessions: SessionStore
    session_access: SessionAccessResolver
    summaries: ExecutionSummaries
    conversations: ConversationStore

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        publisher: TelemetryPublisher | None = None,
        credentials: CredentialResolver | None = None,
        provider_overrides: dict[str, ModelProvider] | None = None,
    ) -> RuntimeContainer:
        db = Database(settings.database_url)
        pools = RedisPools(settings)
        state, cache_redis = pools.get(RedisRole.STATE), pools.get(RedisRole.CACHE)
        scripts = RedisScripts(state)
        http = httpx.AsyncClient()

        bus = InvalidationBus(cache_redis, env=settings.env.value)

        def tiered(family: KeyFamily, l1_ttl_s: float) -> TieredCache:
            cache = TieredCache(
                family,
                l1=L1Cache(50_000),
                l2=RedisL2(cache_redis),
                scripts=scripts,
                l1_ttl_s=l1_ttl_s,
                publisher=bus.publish,
            )
            bus.register([cache])
            return cache

        billing_catalog = load_billing_catalog()
        entitlements = EntitlementService(
            db, cache=tiered(KeyFamily.ENTITLEMENTS, 30), scripts=scripts, catalog=billing_catalog
        )
        api_keys = ApiKeyService(
            db,
            cache=tiered(KeyFamily.API_KEY, 10),
            state=state,
            pepper=settings.api_key_pepper.get_secret_value().encode(),
        )

        stack = build_provider_stack(
            settings,
            db=db,
            http=http,
            entitlements=entitlements,
            byok_cache=tiered(KeyFamily.PROVIDER_CREDENTIAL, 30),
            provider_overrides=provider_overrides,
            credentials=credentials,
        )
        catalog, registry, stub, byok = stack.catalog, stack.registry, stack.stub, stack.byok
        credit_store = CreditService(db)
        credit_gate = CreditGate(credit_store, entitlements, tiered(KeyFamily.CREDIT_BALANCE, 5))
        credit_meter = CreditMeter(credit_store, credit_gate)
        summaries = ExecutionSummaries(state)
        stats = VariantStats(state)
        learner = LearnedSelector(state)
        embedder = _embedder(settings, http)
        long_memory = LongTermMemory(db, vectors=pools.get(RedisRole.VECTOR), embedder=embedder)
        resolver = PolicyResolver(db, cache=tiered(KeyFamily.POLICY, 15))
        if publisher is None:
            publisher = (
                NatsPublisher(settings.nats_url) if settings.nats_enabled else InMemoryPublisher()
            )
        engine = ExecutionEngine(
            catalog=catalog,
            router=Router(catalog),
            registry=registry,
            entitlements=entitlements,
            breaker=CircuitBreaker(state),
            publisher=publisher,
            config=EngineConfig(timeout_s=settings.runtime_timeout_s),
            memory=SessionMemory(state),
            observers=[
                DeployObserver(stats),
                LearningObserver(learner),
                CreditObserver(credit_meter),
                SummaryObserver(summaries),
            ],
            response_cache=ResponseCache(
                cache_redis, pools.get(RedisRole.VECTOR), embedder=embedder
            ),
            long_memory=long_memory,
            learner=learner,
            credit_gate=credit_gate,
        )
        graph_executor, can_search = build_executor(
            settings,
            catalog=catalog,
            registry=registry,
            http=http,
            memory=long_memory,
            checkpoints=WorkingMemory(state),
            credit_gate=credit_gate,
            credit_meter=credit_meter,
        )
        return cls(
            settings,
            db,
            pools,
            scripts,
            http,
            api_keys,
            entitlements,
            billing_catalog,
            engine,
            publisher,
            stub,
            bus,
            byok,
            resolver,
            OutcomeService(stats),
            long_memory,
            PromptRegistry(db, cache=tiered(KeyFamily.PROMPT, 15), entitlements=entitlements),
            GraphRunService(
                db,
                executor=graph_executor,
                entitlements=entitlements,
                envelope=stack.envelope,
                can_search=can_search,
                publish=publisher.publish,
                agents=AgentOrchestrator(
                    graph_executor.llm,
                    reviewer_model=settings.chat_reviewer_model,
                    limits=AgentLimits(
                        max_depth=settings.chat_agent_max_depth,
                        max_turns=settings.chat_agent_max_turns,
                        max_nodes=settings.chat_agent_max_nodes,
                        max_calls=settings.chat_agent_max_calls,
                        max_wall_ms=settings.chat_agent_max_wall_ms,
                    ),
                ),
            ),
            learner,
            credit_gate,
            credit_meter,
            SessionStore(state, ttl_s=settings.session_ttl_s),
            SessionAccessResolver(
                IdentityService(db, region=settings.region),
                tiered(KeyFamily.GENERIC, 10),
            ),
            summaries,
            ConversationStore(db, stack.envelope),
        )

    async def close(self) -> None:
        await self.invalidation.close()
        await self.publisher.close()
        await self.http.aclose()
        await self.redis.close()
        await self.db.dispose()
