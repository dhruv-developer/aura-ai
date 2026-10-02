import random

import pytest

from aura_shared.events import ExecutionCompleted, ExecutionStatus, subject_for
from aura_shared.ids import id_prefix, new_id
from aura_shared.redis import KeyFamily, build_key, ttl_for
from aura_shared.tenancy import AccountKind, PriorityClass, TenantContext


def test_key_layout_has_env_domain_hash_tag_and_schema() -> None:
    key = build_key(KeyFamily.POLICY, "acct_1", "wl_9", env="prod")
    assert key == "aura:prod:policy:{acct_1}:wl_9:v1"


def test_keys_of_one_tenant_share_hash_tag() -> None:
    a = build_key(KeyFamily.QUOTA_USED, "acct_1", "tokens", "2026-10")
    b = build_key(KeyFamily.QUOTA_RESERVATIONS, "acct_1", "tokens", "2026-10")
    assert "{acct_1}" in a and "{acct_1}" in b


@pytest.mark.parametrize("bad", ["", "a:b", "a b", "{x}", "a\nb"])
def test_key_segments_are_validated(bad: str) -> None:
    with pytest.raises(ValueError):
        build_key(KeyFamily.POLICY, "acct_1", bad)
    with pytest.raises(ValueError):
        build_key(KeyFamily.POLICY, bad or "", "x")


def test_every_family_has_a_ttl_entry() -> None:
    for family in KeyFamily:
        ttl_for(family)  # raises KeyError if the registry is missing a family


def test_ttl_jitter_stays_within_ten_percent() -> None:
    rng = random.Random(7)
    samples = {ttl_for(KeyFamily.POLICY, rng=rng) for _ in range(500)}
    assert min(samples) >= 270 and max(samples) <= 330  # type: ignore[type-var]
    assert len(samples) > 10
    assert ttl_for(KeyFamily.RATE_LIMIT) is None


def test_ids_are_prefixed_and_time_sortable() -> None:
    a = new_id("org", now_ms=1_000)
    b = new_id("org", now_ms=2_000)
    assert id_prefix(a) == "org" and a < b
    with pytest.raises(ValueError):
        new_id("nope")


def test_tenant_context_invariants() -> None:
    with pytest.raises(ValueError):
        TenantContext("acct_1", AccountKind.ORGANIZATION, "ws_1", "in", PriorityClass.B2B)
    ctx = TenantContext(
        "acct_1", AccountKind.INDIVIDUAL, "ws_1", "in", PriorityClass.FREE, user_id="usr_1"
    )
    assert ctx.tenant_key == "acct_1"


def test_event_subject_is_partitioned_and_stable() -> None:
    evt = ExecutionCompleted(
        account_id="acct_1",
        region="in",
        trace_id="trc_1",
        execution_id="exe_1",
        workspace_id="ws_1",
        status=ExecutionStatus.SUCCEEDED,
        total_latency_ms=10,
        total_cost=0,
    )
    s1, s2 = subject_for(evt), subject_for(evt)
    assert s1 == s2 and s1.startswith("aura.exec.in.p") and s1.endswith(".execution.completed")
    assert evt.envelope()["schema_version"] == 1
