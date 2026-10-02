"""Drafter ↔ Reviewer → Finaliser query tree, with a scripted fake model."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from aura_graph.agents import AgentLimits, AgentOrchestrator, parse_review
from aura_graph.llm import LlmResult
from aura_providers.domain.types import Message
from aura_shared.errors import DependencyUnavailable

REVIEWER = "groq/openai/gpt-oss-20b"


def review(verdict: str = "approve", issues=(), follow_ups=(), score: int = 4) -> str:  # type: ignore[no-untyped-def]
    return json.dumps(
        {"verdict": verdict, "score": score, "issues": list(issues), "follow_ups": list(follow_ups)}
    )


@dataclass
class Call:
    role: str
    tier: str
    model: str | None
    messages: list[Message]


@dataclass
class FakeLlm:
    """Answers by role (read from the system prompt). Each handler gets the call and
    returns the text."""

    on_review: Callable[[Call], str] = lambda c: review()
    on_draft: Callable[[Call], str] = lambda c: f"answer to: {c.messages[1].content}"
    calls: list[Call] = field(default_factory=list)
    delay: float = 0.0

    async def call(
        self, account_id, *, tier, messages, policy, max_tokens=2000, region="global", model=None
    ):  # type: ignore[no-untyped-def]
        system = messages[0].content
        role = (
            "drafter"
            if system.startswith("You are the Drafter")
            else "reviewer"
            if system.startswith("You are the Reviewer")
            else "finaliser"
        )
        c = Call(role, tier, model, list(messages))
        self.calls.append(c)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = (
            self.on_draft(c)
            if role == "drafter"
            else self.on_review(c)
            if role == "reviewer"
            else "FINAL ANSWER"
        )
        used = model or {"small": "m/small", "medium": "m/medium", "frontier": "m/big"}.get(
            tier, "m/auto"
        )
        return LlmResult(text, Decimal("0.001"), used, 5, 1)

    def by_role(self, role: str) -> list[Call]:
        return [c for c in self.calls if c.role == role]


def orch(llm: FakeLlm, **limits) -> AgentOrchestrator:  # type: ignore[no-untyped-def]
    return AgentOrchestrator(llm, reviewer_model=REVIEWER, limits=AgentLimits(**limits))  # type: ignore[arg-type]


async def test_approved_first_time_reviewer_pinned_and_costs_add_up() -> None:
    llm = FakeLlm()
    events: list[dict] = []  # type: ignore[type-arg]
    res = await orch(llm).run(
        "acct", "Q", subqueries=["What is A here?", "What is B here?"], on_event=events.append
    )
    assert res.answer == "FINAL ANSWER"
    assert [n.status for n in res.nodes()] == ["approved", "approved"]
    assert len(llm.by_role("drafter")) == 2 and len(llm.by_role("reviewer")) == 2
    assert all(c.model == REVIEWER for c in llm.by_role("reviewer"))
    assert res.calls == 5 and res.cost == Decimal("0.005")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "split" and kinds[-1] == "finalised" and kinds.count("review") == 2
    s = res.summary()
    assert s["kind"] == "agents" and s["models"]["reviewer"] == [REVIEWER] and s["revisions"] == 0


async def test_revision_is_a_conversation_with_the_reviewer() -> None:
    attempts: dict[str, int] = {}

    def reviewer(c: Call) -> str:
        n = attempts.get("x", 0) + 1
        attempts["x"] = n
        return review("revise", issues=["mention the unit"]) if n == 1 else review()

    llm = FakeLlm(on_review=reviewer)
    res = await orch(llm).run("acct", "Q", subqueries=["How long is a marathon?"])
    second_draft = llm.by_role("drafter")[1].messages
    assert [m.role for m in second_draft] == ["system", "user", "assistant", "user"]
    assert second_draft[2].content.startswith("answer to:")  # its own earlier draft
    assert "mention the unit" in second_draft[3].content  # the reviewer's critique
    assert res.nodes()[0].status == "approved"
    assert res.answer.startswith("answer to:")  # single approved node: no finaliser call
    assert not llm.by_role("finaliser")


async def test_three_drafts_then_unresolved_goes_to_the_finaliser() -> None:
    llm = FakeLlm(on_review=lambda c: review("revise", issues=["still wrong"]))
    res = await orch(llm).run("acct", "Q", subqueries=["What is hard here?"])
    node = res.nodes()[0]
    assert node.status == "unresolved" and node.issues == ["still wrong"]
    assert len(llm.by_role("drafter")) == 3  # draft + 2 revisions (max_turns=3)
    finaliser_input = llm.by_role("finaliser")[0].messages[1].content
    assert "[UNRESOLVED: still wrong]" in finaliser_input


async def test_bad_reviewer_json_is_retried_then_never_stalls() -> None:
    llm = FakeLlm(on_review=lambda c: "looks fine to me!")
    res = await orch(llm).run("acct", "Q", subqueries=["What is A here?"])
    reviews = [t for t in res.nodes()[0].turns if t.role == "reviewer"]
    assert len(reviews) == 2 and not any(t.parsed for t in reviews)  # one retry
    assert "not valid JSON" in llm.by_role("reviewer")[1].messages[-1].content
    assert res.nodes()[0].status == "approved"


async def test_follow_ups_grow_a_tree_to_depth_three_and_stop() -> None:
    def reviewer(c: Call) -> str:
        q = c.messages[1].content.split("Sub-question: ")[1].split("\n")[0]
        return review(follow_ups=[f"Deeper detail on ({q})?"])

    llm = FakeLlm(on_review=reviewer)
    events: list[dict] = []  # type: ignore[type-arg]
    res = await orch(llm).run("acct", "Q", subqueries=["What is A here?"], on_event=events.append)
    ids = [n.id for n in res.nodes()]
    assert ids == ["1", "1.1", "1.1.1"]  # depth 1 → 2 → 3, then no further
    assert max(n.depth for n in res.nodes()) == 3
    assert res.nodes()[2].parent_question == res.nodes()[1].question
    deepest_review = llm.by_role("reviewer")[-1].messages[1].content
    assert "Do not propose follow-ups" in deepest_review
    assert [e["node"] for e in events if e["type"] == "follow_up"] == ["1.1", "1.1.1"]


async def test_duplicate_follow_ups_collapse() -> None:
    llm = FakeLlm(on_review=lambda c: review(follow_ups=["Same follow-up question?"] * 2))
    res = await orch(llm).run("acct", "Q", subqueries=["What is A here?", "What is B here?"])
    assert sum(1 for n in res.nodes() if n.origin == "follow_up") == 1  # asked once, tree-wide


async def test_node_limit_caps_the_tree() -> None:
    def reviewer(c: Call) -> str:
        q = c.messages[1].content.split("Sub-question: ")[1].split("\n")[0]
        return review(follow_ups=[f"First detail of ({q})?", f"Second detail of ({q})?"])

    llm = FakeLlm(on_review=reviewer)
    res = await orch(llm, max_nodes=4).run(
        "acct", "Q", subqueries=["What is A here?", "What is B here?"]
    )
    assert len(res.nodes()) == 4
    assert any("node limit" in w for w in res.warnings)


async def test_call_limit_still_returns_an_answer() -> None:
    llm = FakeLlm(on_review=lambda c: review("revise", issues=["more"]))
    res = await orch(llm, max_calls=6).run("acct", "Q", subqueries=["What is A?", "What is B?"])
    assert res.answer == "FINAL ANSWER" and res.calls <= 7
    assert {n.status for n in res.nodes()} <= {"unresolved", "skipped_cap"}


async def test_nothing_answered_is_an_error_not_an_empty_reply() -> None:
    llm = FakeLlm()
    with pytest.raises(DependencyUnavailable):
        await orch(llm, max_wall_ms=0).run("acct", "Q", subqueries=["What is A here?"])


async def test_siblings_run_in_parallel() -> None:
    started = asyncio.Event()
    count = {"n": 0}

    async def slow_call(*a, **kw):  # type: ignore[no-untyped-def]
        system = kw["messages"][0].content
        if system.startswith("You are the Drafter"):
            count["n"] += 1
            if count["n"] == 2:
                started.set()
            await asyncio.wait_for(started.wait(), 1)  # deadlocks if run one at a time
        return await FakeLlm.call(llm, *a, **kw)

    llm = FakeLlm()
    llm.call = slow_call  # type: ignore[method-assign]
    res = await orch(llm).run("acct", "Q", subqueries=["What is A here?", "What is B here?"])
    assert len(res.nodes()) == 2


async def test_mode_and_picked_model_drive_the_drafter_only() -> None:
    llm = FakeLlm()
    await orch(llm).run("acct", "Q", subqueries=["What is A here?"], mode="fast")
    assert llm.by_role("drafter")[0].tier == "small"
    llm2 = FakeLlm()
    await orch(llm2).run(
        "acct", "Q", subqueries=["What is A here?"], mode="best", drafter_model="groq/x"
    )
    assert llm2.by_role("drafter")[0].model == "groq/x"
    assert all(c.model == REVIEWER for c in llm2.by_role("reviewer"))


async def test_single_question_becomes_one_reviewed_sub_query() -> None:
    llm = FakeLlm()
    res = await orch(llm).run("acct", "Why is the sky blue?", subqueries=[])
    assert [n.question for n in res.nodes()] == ["Why is the sky blue?"]
    assert len(llm.by_role("reviewer")) == 1


async def test_stop_cancels_the_whole_tree() -> None:
    llm = FakeLlm(delay=0.2)
    task = asyncio.create_task(orch(llm).run("acct", "Q", subqueries=["What is A?", "What is B?"]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert len(llm.calls) == 2  # the two drafts in flight; nothing started afterwards


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"verdict":"approve","score":5,"issues":[],"follow_ups":[]}', ("approve", 5)),
        ('```json\n{"verdict":"revise","issues":["x"],"follow_ups":[]}\n```', ("revise", None)),
        ('{"verdict":"revise","issues":[],"follow_ups":[]}', ("approve", None)),  # nothing to fix
        ('{"verdict":"maybe"}', None),
        ("no json here", None),
        ('{"verdict":"approve","score":9}', ("approve", 5)),  # clamped
    ],
)
def test_parse_review(text: str, expected: tuple[str, int | None] | None) -> None:
    r = parse_review(text)
    assert (None if r is None else (r.verdict, r.score)) == expected


async def test_reviewer_outage_is_reported_never_silently_approved() -> None:
    from aura_providers.domain.types import ProviderError, ProviderErrorKind

    def down(c: Call) -> str:
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "groq 503")

    llm = FakeLlm(on_review=down)
    events: list[dict] = []  # type: ignore[type-arg]
    o = AgentOrchestrator(llm, reviewer_model=REVIEWER, retry_delay_s=0)
    res = await o.run("acct", "Q", subqueries=["What is A here?"], on_event=events.append)
    node = res.nodes()[0]
    assert node.status == "unreviewed" and "reviewer unavailable" in node.issues[0]
    assert len(llm.by_role("reviewer")) == 2  # one retry before giving up
    assert any(e["type"] == "review_failed" for e in events)
    assert any("not reviewed" in w for w in res.summary()["warnings"])
    assert llm.by_role("finaliser")  # not "approved", so the finaliser is told


async def test_a_momentary_reviewer_error_is_retried() -> None:
    from aura_providers.domain.types import ProviderError, ProviderErrorKind

    seen = {"n": 0}

    def flaky(c: Call) -> str:
        seen["n"] += 1
        if seen["n"] == 1:
            raise ProviderError(ProviderErrorKind.RATE_LIMITED, "429")
        return review()

    llm = FakeLlm(on_review=flaky)
    o = AgentOrchestrator(llm, reviewer_model=REVIEWER, retry_delay_s=0)
    res = await o.run("acct", "Q", subqueries=["What is A here?"])
    assert res.nodes()[0].status == "approved"
