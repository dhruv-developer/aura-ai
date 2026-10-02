import pytest
from sqlalchemy import select

from aura_db.models import ProviderCredential
from aura_shared.errors import Conflict, ValidationFailed

KEY = "sk-ant-test-0123456789abcdef"


async def _acct(container, email="b@x.io") -> tuple[str, str]:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    org = await container.identity.create_organization(user.id, "Acme")
    return org.account_id, user.id


async def test_keys_are_sealed_at_rest_and_resolve(container) -> None:  # type: ignore[no-untyped-def]
    svc = container.provider_credentials
    acct, uid = await _acct(container)
    view = await svc.add(
        account_id=acct,
        provider="anthropic",
        api_key=KEY,
        label="prod",
        base_url=None,
        actor_user_id=uid,
    )
    assert view.last4 == "cdef"
    async with container.db.system() as s:
        row = await s.scalar(select(ProviderCredential))
    assert KEY.encode() not in row.ciphertext and KEY.encode() not in row.wrapped_dek
    creds = await svc.resolve(acct, "anthropic")
    assert creds.api_key == KEY and creds.source == "byok"
    assert await svc.resolve(acct, "openai") is None

    with pytest.raises(Conflict):
        await svc.add(
            account_id=acct,
            provider="anthropic",
            api_key=KEY,
            label="",
            base_url=None,
            actor_user_id=uid,
        )
    await svc.revoke(account_id=acct, credential_id=view.id, actor_user_id=uid)
    assert await svc.resolve(acct, "anthropic") is None  # cache invalidated on revoke
    await svc.add(
        account_id=acct,
        provider="anthropic",
        api_key=KEY,
        label="",
        base_url=None,
        actor_user_id=uid,
    )  # a new active key is allowed again


async def test_ciphertext_moved_to_another_tenant_fails(container) -> None:  # type: ignore[no-untyped-def]
    from aura_shared.crypto import DecryptionFailed

    svc = container.provider_credentials
    a, uid = await _acct(container, "a@x.io")
    b, _ = await _acct(container, "c@x.io")
    await svc.add(
        account_id=a, provider="openai", api_key=KEY, label="", base_url=None, actor_user_id=uid
    )
    async with container.db.system() as s:
        row = await s.scalar(select(ProviderCredential).where(ProviderCredential.account_id == a))
        s.add(
            ProviderCredential(
                id="pcr_stolen",
                account_id=b,
                provider="openai",
                label="x",
                key_id=row.key_id,
                wrapped_dek=row.wrapped_dek,
                nonce=row.nonce,
                ciphertext=row.ciphertext,
                last4="x",
            )
        )
    with pytest.raises(DecryptionFailed):
        await svc.resolve(b, "openai")


@pytest.mark.parametrize(
    ("provider", "base_url"), [("nope", None), ("openai", "http://x"), ("openai", "ftp://x")]
)
async def test_input_validation(container, provider, base_url) -> None:  # type: ignore[no-untyped-def]
    acct, uid = await _acct(container)
    with pytest.raises(ValidationFailed):
        await container.provider_credentials.add(
            account_id=acct,
            provider=provider,
            api_key=KEY,
            label="",
            base_url=base_url,
            actor_user_id=uid,
        )
