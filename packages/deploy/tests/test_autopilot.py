from aura_db.models import Account, ShadowRun
from aura_deploy import AutopilotService
from aura_shared.ids import new_id


async def _org(container, email, autopilot=True):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    org = await container.identity.create_organization(user.id, "Acme")
    await container.subscriptions.change_plan(
        account_id=org.account_id, plan_code="growth", actor_type="system", actor_id="t", reason="t"
    )
    async with container.db.system() as s:
        (await s.get(Account, org.account_id)).autopilot = autopilot
        run = ShadowRun(
            id=new_id("shd"),
            account_id=org.account_id,
            name="s",
            status="completed",
            config={
                "workload_key": "tag:support",
                "workload": "support",
                "candidates": ["stub/small"],
            },
            results={"candidates": [{"model": "stub/small", "deploy_recommended": True}]},
        )
        s.add(run)
    return org, run.id


async def test_autopilot_deploys_proven_change_as_canary_once(container) -> None:  # type: ignore[no-untyped-def]
    org, run_id = await _org(container, "ap@acme.io")
    off, _ = await _org(container, "manual@other.io", autopilot=False)
    autopilot = AutopilotService(container.db, policies=container.policies)
    actions = await autopilot.tick()
    assert [a["account_id"] for a in actions] == [org.account_id]
    action = actions[0]
    assert (
        action["workload"] == "support" and action["status"] == "canary" and action["percent"] == 1
    )
    [pol] = await container.policies.list(org.account_id)
    assert pol["scope_ref"] == "support" and pol["deployments"][0]["status"] == "canary"
    version = next(v for v in pol["versions"] if v["status"] == "canary")
    assert version["evidence"]["shadow_run_id"] == run_id and version["evidence"]["forced"] is False
    assert await autopilot.tick() == []  # each shadow run is consumed once
    assert await container.policies.list(off.account_id) == []  # autopilot off: untouched
