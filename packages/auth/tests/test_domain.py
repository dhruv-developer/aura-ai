import pytest

from aura_auth.domain import api_keys
from aura_auth.domain.emails import normalize_email, slugify
from aura_auth.domain.rbac import (
    API_KEY_GRANTABLE,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    permissions_for,
    require,
    validate_api_key_permissions,
)
from aura_shared.errors import Forbidden, Unauthenticated, ValidationFailed

PEPPER = b"pepper"


def test_role_hierarchy_is_monotonic() -> None:
    viewer, analyst = permissions_for(Role.VIEWER), permissions_for(Role.ANALYST)
    engineer, admin, owner = (permissions_for(r) for r in (Role.ENGINEER, Role.ADMIN, Role.OWNER))
    assert viewer < analyst < engineer < admin < owner
    assert owner == frozenset(Permission)
    assert Permission.ACCOUNT_MANAGE not in admin  # owner-only
    assert Permission.BILLING_MANAGE in permissions_for(Role.BILLING)
    assert Permission.BILLING_MANAGE not in engineer
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_require_raises_forbidden() -> None:
    require(permissions_for(Role.VIEWER), Permission.ACCOUNT_READ)
    with pytest.raises(Forbidden):
        require(permissions_for(Role.VIEWER), Permission.API_KEYS_MANAGE)


def test_api_key_permissions_cannot_escalate() -> None:
    engineer = permissions_for(Role.ENGINEER)
    assert validate_api_key_permissions({"runtime:execute"}, engineer) == {Permission.EXECUTE}
    with pytest.raises(ValidationFailed):  # never grantable to machines
        validate_api_key_permissions({"billing:manage"}, permissions_for(Role.OWNER))
    with pytest.raises(ValidationFailed):
        validate_api_key_permissions({"nope"}, engineer)
    with pytest.raises(ValidationFailed):
        validate_api_key_permissions(set(), engineer)
    with pytest.raises(Forbidden):  # a viewer cannot mint an execute key
        validate_api_key_permissions({"runtime:execute"}, permissions_for(Role.VIEWER))
    assert all(p in permissions_for(Role.OWNER) for p in API_KEY_GRANTABLE)


def test_api_key_roundtrip_and_tamper_detection() -> None:
    for env in ("development", "staging", "production", None):
        k = api_keys.generate(env, PEPPER)
        parsed = api_keys.parse(k.plaintext)
        assert parsed.prefix == k.prefix and api_keys.verify(parsed, k.secret_hash, PEPPER)
        last = "A" if k.plaintext[-1] != "A" else "B"
        tampered = api_keys.parse(k.plaintext[:-1] + last)
        assert not api_keys.verify(tampered, k.secret_hash, PEPPER)
        assert not api_keys.verify(parsed, k.secret_hash, b"other-pepper")


def test_api_key_secret_may_contain_underscores() -> None:
    parsed = api_keys.parse("aura_sk_prod_abcdefghijkl_" + "a_b-" * 10)
    assert parsed.prefix == "abcdefghijkl" and parsed.env == "prod"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "sk_live_x",
        "aura_sk_xx_abcdefghijkl_" + "a" * 40,
        "aura_sk_prod_short_" + "a" * 40,
        "aura_sk_prod_abcdefghijkl_short",
    ],
)
def test_malformed_keys_rejected(bad: str) -> None:
    with pytest.raises(Unauthenticated):
        api_keys.parse(bad)


def test_email_and_slug() -> None:
    assert normalize_email("  Dev@Example.COM ") == "dev@example.com"
    for bad in ["nope", "a@b", "a b@c.io", "@x.io"]:
        with pytest.raises(ValidationFailed):
            normalize_email(bad)
    assert slugify("Acme  AI, Inc.") == "acme-ai-inc"
    assert slugify("!!!") == "org"


def test_totp_rfc6238_vector_window_and_garbage() -> None:
    import base64

    from aura_auth.domain import totp

    secret = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 SHA-1 seed
    assert totp.code_at(secret, 59 // 30) == "287082"  # RFC: 94287082 (8 digits)
    assert totp.code_at(secret, 1111111109 // 30) == "081804"  # RFC: 07081804
    assert totp.verify(secret, "287082", now=59) == 1
    assert totp.verify(secret, "287082", now=59 + 30) == 1  # one step of drift allowed
    assert totp.verify(secret, "287082", now=59 + 90) is None
    for bad in ["", "12345", "abcdef", "1234567"]:
        assert totp.verify(secret, bad, now=59) is None
    assert totp.provisioning_uri(secret, "a@b.io").startswith(
        "otpauth://totp/AURA%20Admin:a%40b.io?"
    )
