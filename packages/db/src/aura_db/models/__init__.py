"""All ORM models. Importing this module registers every table on ``Base.metadata``."""

from aura_db.models.admin import AdminMfa
from aura_db.models.commercial import (
    CreditWallet,
    EntitlementOverride,
    Feature,
    Invoice,
    LedgerEntry,
    LedgerTransaction,
    PaymentEvent,
    Plan,
    PlanEntitlement,
    PlanVersion,
    Subscription,
    UsageRecord,
)
from aura_db.models.conversations import Conversation, ConversationMessage
from aura_db.models.graphs import GraphRun
from aura_db.models.identity import (
    Account,
    ApiKey,
    CustomRole,
    Environment,
    Membership,
    OAuthIdentity,
    Organization,
    Project,
    User,
    Workspace,
)
from aura_db.models.integrations import ProviderCredential
from aura_db.models.jobs import Job, ShadowRun
from aura_db.models.memory import MemoryItem
from aura_db.models.ops import AuditLog, OutboxMessage
from aura_db.models.policies import Deployment, ExecutionPolicyRow, PolicyVersion
from aura_db.models.prompts import PromptOptimization, PromptTemplate, PromptVersion
from aura_db.models.sharing import SharedCard
from aura_db.models.sso import ScimToken, SsoConnection, VerifiedDomain
from aura_db.models.xray import Lead, XRayReport

# Tables that carry account_id and are protected by Row-Level Security.
TENANT_TABLES: tuple[str, ...] = (
    "workspaces",
    "projects",
    "environments",
    "api_keys",
    "subscriptions",
    "entitlement_overrides",
    "credit_wallets",
    "ledger_transactions",
    "ledger_entries",
    "usage_records",
    "invoices",
    "provider_credentials",
    "xray_reports",
    "shadow_runs",
    "execution_policies",
    "policy_versions",
    "deployments",
    "memory_items",
    "prompt_templates",
    "prompt_versions",
    "prompt_optimizations",
    "graph_runs",
    "shared_cards",
    "custom_roles",
    "verified_domains",
    "sso_connections",
    "scim_tokens",
    "conversations",
    "conversation_messages",
)

__all__ = [
    "TENANT_TABLES",
    "Account",
    "AdminMfa",
    "ApiKey",
    "AuditLog",
    "Conversation",
    "ConversationMessage",
    "CreditWallet",
    "CustomRole",
    "Deployment",
    "EntitlementOverride",
    "Environment",
    "ExecutionPolicyRow",
    "Feature",
    "GraphRun",
    "Invoice",
    "Job",
    "Lead",
    "LedgerEntry",
    "LedgerTransaction",
    "Membership",
    "MemoryItem",
    "OAuthIdentity",
    "Organization",
    "OutboxMessage",
    "PaymentEvent",
    "Plan",
    "PlanEntitlement",
    "PlanVersion",
    "PolicyVersion",
    "Project",
    "PromptOptimization",
    "PromptTemplate",
    "PromptVersion",
    "ProviderCredential",
    "ScimToken",
    "ShadowRun",
    "SharedCard",
    "SsoConnection",
    "Subscription",
    "UsageRecord",
    "User",
    "VerifiedDomain",
    "Workspace",
    "XRayReport",
]
