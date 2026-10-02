"""Composition root: builds adapters and wires them into application services."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from aura_auth.adapters.google import GoogleOAuth
from aura_auth.adapters.mailer import LogMailer, SmtpMailer
from aura_auth.application.admin_mfa import AdminMfaService
from aura_auth.application.api_keys import ApiKeyService
from aura_auth.application.identity import IdentityService
from aura_auth.application.magic_link import MagicLinkService
from aura_auth.application.members import MemberService
from aura_auth.application.oauth import OAuthLoginService
from aura_auth.application.ports import Mailer, OAuthProvider
from aura_auth.application.sessions import SessionStore
from aura_billing.adapters.razorpay_gateway import RazorpayGateway
from aura_billing.application.consumer import CreditGate, MonthlyGrants, ReferralService
from aura_billing.application.credits import CreditService
from aura_billing.application.economics import UnitEconomics
from aura_billing.application.entitlements import EntitlementService
from aura_billing.application.ports import PaymentGateway
from aura_billing.application.subscriptions import SubscriptionService
from aura_billing.application.usage import UsageReconciler
from aura_billing.domain.catalog import Catalog, load_catalog
from aura_cache import InvalidationBus, L1Cache, RedisL2, TieredCache
from aura_credentials import ProviderCredentialService
from aura_db import Database
from aura_deploy import PolicyService, RecommendationService, VariantStats
from aura_engine.sharing import ExecutionSummaries, ShareService
from aura_engine.wiring import build_envelope
from aura_prompts import PromptOptimizerService, PromptRegistry
from aura_providers.domain.catalog import load_catalog as load_model_catalog
from aura_shadow import ShadowService
from aura_shared.crypto import envelope_from_settings
from aura_shared.redis import KeyFamily, RedisPools, RedisRole, RedisScripts
from aura_shared.settings import Environment, Settings
from aura_xray.adapters.clickhouse_source import ClickHouseTrafficSource
from aura_xray.application.service import XRayService


@dataclass
class Container:
    settings: Settings
    db: Database
    redis: RedisPools
    http: httpx.AsyncClient
    catalog: Catalog
    identity: IdentityService
    sessions: SessionStore
    magic_links: MagicLinkService
    oauth: OAuthLoginService
    api_keys: ApiKeyService
    entitlements: EntitlementService
    subscriptions: SubscriptionService
    credits: CreditService
    usage: UsageReconciler
    invalidation: InvalidationBus
    provider_credentials: ProviderCredentialService
    xray: XRayService
    shadow: ShadowService
    admin_mfa: AdminMfaService
    policies: PolicyService
    recommendations: RecommendationService
    variant_stats: VariantStats
    prompts: PromptRegistry
    prompt_optimizer: PromptOptimizerService
    traffic: ClickHouseTrafficSource
    scripts: RedisScripts
    credit_gate: CreditGate
    referrals: ReferralService
    grants: MonthlyGrants
    economics: UnitEconomics
    shares: ShareService
    members: MemberService

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        mailer: Mailer | None = None,
        gateway: PaymentGateway | None = None,
        oauth_providers: dict[str, OAuthProvider] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> Container:
        db = Database(settings.database_url)
        pools = RedisPools(settings)
        state, cache_redis = pools.get(RedisRole.STATE), pools.get(RedisRole.CACHE)
        scripts = RedisScripts(state)  # single-flight leases live in the state role
        http = http or httpx.AsyncClient()
        catalog = load_catalog()

        bus = InvalidationBus(cache_redis, env=settings.env.value)

        def tiered(family: KeyFamily, l1_ttl_s: float) -> TieredCache:
            cache = TieredCache(
                family,
                l1=L1Cache(10_000),
                l2=RedisL2(cache_redis),
                scripts=scripts,
                l1_ttl_s=l1_ttl_s,
                publisher=bus.publish,
            )
            bus.register([cache])
            return cache

        entitlements = EntitlementService(
            db, cache=tiered(KeyFamily.ENTITLEMENTS, 30), scripts=scripts, catalog=catalog
        )
        if gateway is None and settings.razorpay_key_id and settings.razorpay_key_secret:
            gateway = RazorpayGateway(
                settings.razorpay_key_id, settings.razorpay_key_secret.get_secret_value(), http
            )
        credits = CreditService(db)
        credit_gate = CreditGate(credits, entitlements, tiered(KeyFamily.CREDIT_BALANCE, 5))
        subscriptions = SubscriptionService(
            db,
            credits=credits,
            on_credits_changed=credit_gate.invalidate,
            catalog=catalog,
            entitlements=entitlements,
            gateway=gateway,
            webhook_secret=(
                settings.razorpay_webhook_secret.get_secret_value()
                if settings.razorpay_webhook_secret
                else None
            ),
        )
        grants = MonthlyGrants(db, credits, credit_gate, entitlements, catalog)
        identity = IdentityService(
            db,
            region=settings.region,
            hooks=[subscriptions.on_account_created, grants.on_account_created],
        )
        if oauth_providers is None:
            oauth_providers = {}
            if settings.google_client_id and settings.google_client_secret:
                oauth_providers["google"] = GoogleOAuth(
                    settings.google_client_id,
                    settings.google_client_secret.get_secret_value(),
                    http,
                )
        policies = PolicyService(
            db, entitlements=entitlements, resolver_cache=tiered(KeyFamily.POLICY, 15)
        )
        return cls(
            settings=settings,
            db=db,
            redis=pools,
            http=http,
            catalog=catalog,
            identity=identity,
            sessions=SessionStore(state, ttl_s=settings.session_ttl_s),
            magic_links=MagicLinkService(
                state=state,
                scripts=scripts,
                identity=identity,
                mailer=mailer
                or (
                    LogMailer()
                    if settings.mail_backend == "log"
                    and settings.env in (Environment.DEV, Environment.TEST)
                    else SmtpMailer(settings.smtp_host, settings.smtp_port, settings.mail_from)
                ),
                verify_url=f"{settings.public_web_url.rstrip('/')}/auth/verify",
                app_verify_urls={"admin": f"{settings.public_admin_url.rstrip('/')}/auth/verify"},
            ),
            oauth=OAuthLoginService(
                state=state,
                identity=identity,
                providers=oauth_providers,
                redirect_base=f"{settings.public_api_url.rstrip('/')}/v1",
            ),
            api_keys=ApiKeyService(
                db,
                cache=tiered(KeyFamily.API_KEY, 10),
                state=state,
                pepper=settings.api_key_pepper.get_secret_value().encode(),
            ),
            entitlements=entitlements,
            subscriptions=subscriptions,
            credits=credits,
            credit_gate=credit_gate,
            referrals=ReferralService(db, credits, credit_gate),
            grants=grants,
            economics=UnitEconomics(db),
            shares=ShareService(db, ExecutionSummaries(state)),
            members=MemberService(db, identity),
            usage=UsageReconciler(db, scripts=scripts, entitlements=entitlements, catalog=catalog),
            invalidation=bus,
            xray=XRayService(db, catalog=load_model_catalog()),
            admin_mfa=AdminMfaService(db, envelope=build_envelope(settings), state=state),
            prompts=PromptRegistry(
                db, cache=tiered(KeyFamily.PROMPT, 15), entitlements=entitlements
            ),
            prompt_optimizer=PromptOptimizerService(
                db,
                envelope=build_envelope(settings),
                catalog=load_model_catalog(),
                entitlements=entitlements,
            ),
            variant_stats=VariantStats(state),
            recommendations=RecommendationService(db, policies=policies),
            policies=policies,
            shadow=ShadowService(
                db,
                envelope=build_envelope(settings),
                catalog=load_model_catalog(),
                entitlements=entitlements,
            ),
            traffic=ClickHouseTrafficSource(http, settings.clickhouse_url),
            scripts=scripts,
            provider_credentials=ProviderCredentialService(
                db,
                envelope=envelope_from_settings(
                    settings.master_key.get_secret_value() if settings.master_key else None,
                    allow_ephemeral=settings.env in (Environment.DEV, Environment.TEST),
                ),
                cache=tiered(KeyFamily.PROVIDER_CREDENTIAL, 30),
            ),
        )

    async def close(self) -> None:
        await self.invalidation.close()
        await self.http.aclose()
        await self.redis.close()
        await self.db.dispose()
