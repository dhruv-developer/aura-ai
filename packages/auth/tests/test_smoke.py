"""End-to-end smoke: signup → personal account → default plan → entitlements via RLS."""


async def test_signup_yields_free_plan_entitlements(container) -> None:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("a@x.io")
    [personal] = await container.identity.list_access(user.id)
    ents = await container.entitlements.get(personal.account_id)
    assert ents.plan_code == "free" and ents.allows("basic_routing")
    assert not ents.allows("shadow_optimization")
