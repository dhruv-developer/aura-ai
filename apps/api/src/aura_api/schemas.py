"""HTTP request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MagicLinkRequest(BaseModel):
    email: str = Field(max_length=320)
    app: Literal["web", "admin"] = "web"


class MagicLinkVerify(BaseModel):
    token: str = Field(max_length=200)


class UserOut(_Out):
    id: str
    email: str
    name: str | None
    is_superadmin: bool


class AccountAccessOut(_Out):
    account_id: str
    kind: str
    tier: str
    name: str
    role: str
    organization_id: str | None
    slug: str | None
    permissions: list[str]


class MeOut(BaseModel):
    user: UserOut
    accounts: list[AccountAccessOut]


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)


class OrganizationOut(_Out):
    id: str
    account_id: str
    name: str
    slug: str
    workspace_id: str
    project_id: str
    environment_ids: dict[str, str]


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    permissions: list[str] | None = None
    environment_id: str | None = None
    expires_at: datetime | None = None


class ApiKeyOut(_Out):
    id: str
    name: str
    prefix: str
    environment_id: str | None
    permissions: list[str]
    created_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Shown once. Store it securely; AURA cannot show it again.")


class ProviderCredentialCreate(BaseModel):
    provider: str = Field(max_length=40)
    api_key: str = Field(min_length=16, max_length=512, repr=False)
    label: str = Field(default="", max_length=120)
    base_url: str | None = Field(default=None, max_length=500)


class ProviderCredentialOut(_Out):
    id: str
    provider: str
    label: str
    base_url: str | None
    last4: str
    created_at: datetime | None
    revoked_at: datetime | None


class EntitlementOut(BaseModel):
    feature: str
    enabled: bool
    limit: int | None
    period: str | None
    used: int | None = None


class PlanOut(BaseModel):
    code: str
    name: str
    audience: str
    price_minor: int | None
    currency: str
    interval: str
    trial_days: int
    entitlements: list[EntitlementOut]


class SubscriptionOut(_Out):
    id: str
    plan_code: str
    plan_name: str
    plan_version: int
    status: str
    price_minor: int | None
    currency: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_ends_at: datetime | None
    cancel_at_period_end: bool


class CheckoutRequest(BaseModel):
    plan: str = Field(max_length=40)


class CheckoutOut(BaseModel):
    checkout_url: str


class LedgerLineOut(_Out):
    transaction_id: str
    kind: str
    amount: int
    memo: str
    reference: str | None
    created_at: datetime


class CreditsOut(BaseModel):
    balance: int
    history: list[LedgerLineOut]


class CreditPackOut(BaseModel):
    code: str
    name: str
    credits: int
    price_minor: int
    currency: str


class CreditPurchaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: str = Field(min_length=1, max_length=40)


class CreditOrderOut(BaseModel):
    provider: str
    order_id: str
    amount_minor: int
    currency: str
    key_id: str


class ReferralOut(BaseModel):
    code: str
    referred: int
    credits_earned: int


class ReferralRedeemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=4, max_length=16)


class ShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=4, max_length=40)


class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    role: str = "viewer"
    custom_role_id: str | None = None


class MemberRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    custom_role_id: str | None = None


class CustomRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=60)
    description: str = Field(default="", max_length=300)
    permissions: list[str] = Field(min_length=1, max_length=32)
