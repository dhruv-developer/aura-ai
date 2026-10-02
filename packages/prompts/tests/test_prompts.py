import pytest

from aura_db.jobs import JobQueue
from aura_db.models import PromptOptimization
from aura_prompts import PromptOptimizationRunner, candidates, render
from aura_prompts.optimizer import JOB_KIND
from aura_prompts.templates import variables
from aura_shared.errors import Conflict, NotFound, ValidationFailed
from aura_workers.jobs_runner import JobRunner

VERBOSE = """You are a support assistant for {{company}}.


I want you to answer questions about orders.   Please note that you should be very polite.
It is very important that you cite the order number.
It is very important that you cite the order number.
In order to help, make sure you always ask for the order number if missing."""


def test_render_and_variables() -> None:
    assert render("Hi {{ name }} from {{company}}", {"name": "A", "company": "B"}) == "Hi A from B"
    assert variables(VERBOSE) == {"company"}
    with pytest.raises(ValidationFailed, match="company"):
        render(VERBOSE, {})


def test_candidates_preserve_variables_and_shrink() -> None:
    cands = dict(candidates(VERBOSE))
    assert {"whitespace", "dedupe", "filler", "concise", "combined"} <= set(cands)
    for text in cands.values():
        assert variables(text) == {"company"}
    assert cands["dedupe"].count("cite the order number") == 1
    assert "Please note" not in cands["filler"] and "I want you to" not in cands["filler"]
    assert len(cands["combined"]) < len(VERBOSE)
    assert candidates("Be brief.") == [] or all(
        t != "Be brief." for _, t in candidates("Be brief.")
    )


async def _org(container):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("pm@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    return user, org


async def test_registry_activate_resolve_and_rollback(container) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    ids = await container.prompts.create(
        org.account_id, name="support.v", content="V1 {{x}}", actor_id=user.id
    )
    assert (await container.prompts.resolve(org.account_id, "support.v")).content == "V1 {{x}}"
    v2 = await container.prompts.add_version(
        org.account_id, template_id=ids["template_id"], content="V2 {{x}}", actor_id=user.id
    )
    assert (await container.prompts.resolve(org.account_id, "support.v")).version == 1  # draft
    await container.prompts.activate(
        org.account_id, template_id=ids["template_id"], version_id=v2, actor_id=user.id
    )
    assert (await container.prompts.resolve(org.account_id, "support.v")).content == "V2 {{x}}"
    await container.prompts.activate(
        org.account_id,
        template_id=ids["template_id"],
        version_id=ids["version_id"],
        actor_id=user.id,
    )  # rollback
    assert (await container.prompts.resolve(org.account_id, "support.v")).version == 1
    with pytest.raises(Conflict):
        await container.prompts.create(
            org.account_id, name="support.v", content="x", actor_id=user.id
        )
    with pytest.raises(ValidationFailed):
        await container.prompts.create(
            org.account_id, name="Bad Name", content="x", actor_id=user.id
        )
    with pytest.raises(NotFound):
        await container.prompts.resolve(org.account_id, "missing")


async def test_optimizer_recommends_leaner_prompt_at_equal_quality(container, runtime) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    ids = await container.prompts.create(
        org.account_id, name="support", content=VERBOSE, actor_id=user.id
    )
    items = [{"input": f"where is order {i}?", "vars": {"company": "Acme"}} for i in range(40)]
    started = await container.prompt_optimizer.start(
        org.account_id,
        template_id=ids["template_id"],
        model="stub/small",
        items=items,
        actor_id=user.id,
    )
    runner = PromptOptimizationRunner(
        container.db,
        envelope=container.prompt_optimizer._envelope,
        catalog=runtime.engine.catalog,
        registry=runtime.engine._registry,
        prompts=container.prompts,
    )
    assert await JobRunner(JobQueue(container.db), {JOB_KIND: runner.process}).run_once()
    run = await container.prompt_optimizer.get(org.account_id, started["run_id"])
    assert run["status"] == "completed", run["error"]
    best = run["results"]["candidates"][0]
    # The stub's answer does not depend on the system prompt, so quality is equal and the
    # shortest candidate wins on tokens.
    assert best["recommended"] and best["prompt_token_savings_pct"] > 5
    listed = await container.prompts.list(org.account_id)
    drafts = [v for v in listed[0]["versions"] if v["origin"].startswith("optimizer:")]
    assert drafts and all(v["status"] == "draft" for v in drafts)  # never auto-activated
    async with container.db.system() as s:
        row = await s.get(PromptOptimization, started["run_id"])
    assert row.dataset_ciphertext is None  # eval inputs erased
    with pytest.raises(ValidationFailed, match="missing variables"):
        await container.prompt_optimizer.start(
            org.account_id,
            template_id=ids["template_id"],
            model="stub/small",
            items=[{"input": "x", "vars": {}}],
            actor_id=user.id,
        )
