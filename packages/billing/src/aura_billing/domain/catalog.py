"""Plan catalog model, loading and validation. Pure: no I/O beyond reading the YAML."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from aura_billing.domain.entitlements import Entitlement

FEATURE_KINDS = frozenset({"boolean", "metered", "gauge"})
PERIODS = frozenset({"day", "month"})
PRIORITY_CLASSES = frozenset({"free", "paid", "b2b", "enterprise"})


class CatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    key: str
    kind: str
    unit: str | None
    description: str


@dataclass(frozen=True, slots=True)
class PlanSpec:
    code: str
    name: str
    audience: str
    priority_class: str
    price_minor: int | None
    currency: str
    interval: str
    trial_days: int
    is_public: bool
    sort_order: int
    entitlements: tuple[Entitlement, ...]

    def content_hash(self) -> str:
        """Changes when anything customer-affecting changes, which triggers a new version."""
        body = {
            "price_minor": self.price_minor,
            "currency": self.currency,
            "interval": self.interval,
            "trial_days": self.trial_days,
            "entitlements": sorted(
                [e.feature, e.enabled, e.limit, e.period] for e in self.entitlements
            ),
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CreditPackSpec:
    """A one-off credit top-up (consumer accounts)."""

    code: str
    name: str
    credits: int
    price_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class Catalog:
    features: dict[str, FeatureSpec]
    plans: dict[str, PlanSpec]
    defaults: dict[str, str] = field(default_factory=dict)
    credit_packs: dict[str, CreditPackSpec] = field(default_factory=dict)

    def default_plan_for(self, account_kind: str) -> PlanSpec:
        return self.plans[self.defaults[account_kind]]


def _entitlement(feature: FeatureSpec, raw: Any, plan: str) -> Entitlement:
    where = f"plan {plan!r} feature {feature.key!r}"
    if feature.kind == "boolean":
        if not isinstance(raw, bool):
            raise CatalogError(f"{where}: boolean features take true/false")
        return Entitlement(feature.key, raw, None, None)
    if not isinstance(raw, dict) or "limit" not in raw:
        raise CatalogError(f"{where}: {feature.kind} features take {{limit: n|null, ...}}")
    limit = raw["limit"]
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise CatalogError(f"{where}: limit must be a non-negative int or null")
    period = raw.get("period")
    if feature.kind == "metered" and period not in PERIODS:
        raise CatalogError(f"{where}: metered features need period in {sorted(PERIODS)}")
    if feature.kind == "gauge" and period is not None:
        raise CatalogError(f"{where}: gauges have no period")
    return Entitlement(feature.key, bool(raw.get("enabled", True)), limit, period)


def parse_catalog(data: dict[str, Any]) -> Catalog:
    features: dict[str, FeatureSpec] = {}
    for key, spec in data["features"].items():
        if spec["kind"] not in FEATURE_KINDS:
            raise CatalogError(f"feature {key!r}: unknown kind {spec['kind']!r}")
        features[key] = FeatureSpec(
            key, spec["kind"], spec.get("unit"), spec.get("description", "")
        )

    plans: dict[str, PlanSpec] = {}
    for code, spec in data["plans"].items():
        if spec["audience"] not in ("b2c", "b2b"):
            raise CatalogError(f"plan {code!r}: audience must be b2c or b2b")
        if spec["priority_class"] not in PRIORITY_CLASSES:
            raise CatalogError(f"plan {code!r}: unknown priority_class")
        ents = []
        for fkey, raw in (spec.get("entitlements") or {}).items():
            if fkey not in features:
                raise CatalogError(f"plan {code!r}: unknown feature {fkey!r}")
            ents.append(_entitlement(features[fkey], raw, code))
        plans[code] = PlanSpec(
            code=code,
            name=spec["name"],
            audience=spec["audience"],
            priority_class=spec["priority_class"],
            price_minor=spec.get("price_minor"),
            currency=spec.get("currency", "INR"),
            interval=spec.get("interval", "month"),
            trial_days=int(spec.get("trial_days", 0)),
            is_public=bool(spec.get("is_public", True)),
            sort_order=int(spec.get("sort_order", 0)),
            entitlements=tuple(sorted(ents, key=lambda e: e.feature)),
        )

    defaults = dict(data.get("defaults") or {})
    for kind, code in defaults.items():
        if code not in plans:
            raise CatalogError(f"default plan for {kind!r} is unknown: {code!r}")
    audiences = {"individual": "b2c", "organization": "b2b"}
    for kind, code in defaults.items():
        if plans[code].audience != audiences.get(kind):
            raise CatalogError(f"default plan {code!r} does not match audience for {kind!r}")
    packs: dict[str, CreditPackSpec] = {}
    for code, spec in (data.get("credit_packs") or {}).items():
        credits, price = spec.get("credits"), spec.get("price_minor")
        if not isinstance(credits, int) or credits <= 0:
            raise CatalogError(f"credit pack {code!r}: credits must be a positive int")
        if not isinstance(price, int) or price <= 0:
            raise CatalogError(f"credit pack {code!r}: price_minor must be a positive int")
        packs[code] = CreditPackSpec(
            code, spec.get("name", code), credits, price, spec.get("currency", "INR")
        )
    return Catalog(features, plans, defaults, packs)


def load_catalog(path: Path | None = None) -> Catalog:
    text = (
        path.read_text("utf-8")
        if path
        else (resources.files("aura_billing") / "catalog.yaml").read_text("utf-8")
    )
    return parse_catalog(yaml.safe_load(text))
