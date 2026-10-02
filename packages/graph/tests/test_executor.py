from decimal import Decimal

import pytest

from aura_graph.executor import GraphExecutor, RunContext
from aura_graph.intent import compile_intent
from aura_graph.ir import Graph, Limits, Node, NodeKind
from aura_graph.llm import RoutedLlm
from aura_graph.planner import plan
from aura_graph.tools import CalculatorTool, ToolRegistry
from aura_memory import LongTermMemory, WorkingMemory
from aura_providers.domain.types import ProviderErrorKind
from aura_providers.embeddings import HashingEmbedder
from aura_routing import Router


class FlakyTool:
    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.calls, self._fail = 0, fail_times

    async def run(self, account_id, args):  # type: ignore[no-untyped-def]
        from aura_graph.tools import ToolResult

        self.calls += 1
        return ToolResult("boom", ok=False) if self.calls <= self._fail else ToolResult("data")


@pytest.fixture
async def env(container, runtime, redis_client):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("g@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    catalog = runtime.engine.catalog
    llm = RoutedLlm(catalog=catalog, router=Router(catalog), registry=runtime.engine._registry)
    memory = LongTermMemory(container.db, vectors=redis_client, embedder=HashingEmbedder())
    flaky = FlakyTool(fail_times=1)

    def make(limits: Limits | None = None) -> GraphExecutor:
        return GraphExecutor(
            llm=llm,
            tools=ToolRegistry([CalculatorTool(), flaky]),
            memory=memory,
            checkpoints=WorkingMemory(redis_client),
            limits=limits,
        )

    return {
        "acct": org.account_id,
        "make": make,
        "memory": memory,
        "stub": runtime.stub,
        "flaky": flaky,
    }


def _ctx(env, goal: str, run_id: str = "run_1", **kw) -> RunContext:  # type: ignore[no-untyped-def]
    return RunContext(env["acct"], run_id, goal, **kw)


async def test_math_goal_needs_no_model(env) -> None:  # type: ignore[no-untyped-def]
    goal = "what is 12 * (3 + 4)?"
    result = await env["make"]().run(plan(compile_intent(goal), goal=goal), _ctx(env, goal))
    assert result.status == "completed" and result.output == "84"
    assert env["stub"].calls == [] and result.cost == 0


async def test_knowledge_qa_retrieves_answers_and_verifies(env) -> None:  # type: ignore[no-untyped-def]
    await env["memory"].add(
        env["acct"],
        scope="kb",
        scope_ref="handbook",
        content="Annual plans can be refunded within 30 days of purchase.",
    )
    goal = "what is the refund policy for annual plans?"
    g = plan(compile_intent(goal, has_knowledge=True), goal=goal, kb_scopes=[("kb", "handbook")])
    result = await env["make"]().run(g, _ctx(env, goal))
    assert result.status == "completed", result.warnings
    assert "30 days" in result.output  # stub echoes its prompt, which includes the sources
    names = [e["event"] for e in result.events]
    for expected in (
        "TASK_CREATED",
        "CONTEXT_SELECTED",
        "MODEL_SELECTED",
        "VERIFICATION_COMPLETED",
        "EXECUTION_COMPLETED",
    ):
        assert expected in names
    assert result.trace()["nodes"][1]["model"].startswith("stub/")


class JudgeSays:
    """Wraps the real LLM caller; the strong-verification judge returns a fixed verdict."""

    def __init__(self, inner, verdict: str) -> None:  # type: ignore[no-untyped-def]
        self.inner, self.verdict, self.judged = inner, verdict, 0

    async def call(self, account_id, *, tier, messages, policy, max_tokens=2000, region="global"):  # type: ignore[no-untyped-def]
        from aura_graph.llm import LlmResult

        if messages[0].content.startswith("You check answers"):
            self.judged += 1
            return LlmResult(self.verdict, Decimal("0.0001"), "judge", 1, 1)
        return await self.inner.call(
            account_id,
            tier=tier,
            messages=messages,
            policy=policy,
            max_tokens=max_tokens,
            region=region,
        )


async def test_high_risk_strong_verification_escalates(env) -> None:  # type: ignore[no-untyped-def]
    goal = "Analyze the legal contract risks"
    g = plan(compile_intent(goal), goal=goal)
    base = env["make"]()
    judge = JudgeSays(base._llm, "UNSUPPORTED")
    base._llm = judge
    result = await base.run(g, _ctx(env, goal))
    assert result.escalations == 2
    assert any("verification failed after escalation" in w for w in result.warnings)
    escalations = [e for e in result.events if e["event"] == "ESCALATION_REQUIRED"]
    assert [e["to_tier"] for e in escalations] == ["frontier", "frontier"]
    assert result.output  # best answer still returned, with a caveat
    assert judge.judged == 3  # initial check + one per escalation

    passing = env["make"]()
    passing._llm = JudgeSays(passing._llm, "SUPPORTED")
    ok = await passing.run(g, _ctx(env, goal, run_id="run_ok"))
    assert ok.escalations == 0 and ok.status == "completed" and not ok.warnings


async def test_checkpoints_make_resume_free(env) -> None:  # type: ignore[no-untyped-def]
    goal = "Research the market for EV chargers"
    g = plan(compile_intent(goal), goal=goal)
    first = await env["make"]().run(g, _ctx(env, goal, run_id="run_resume"))
    calls_after_first = len(env["stub"].calls)
    second = await env["make"]().run(g, _ctx(env, goal, run_id="run_resume"))
    assert second.output == first.output
    assert len(env["stub"].calls) == calls_after_first  # nothing re-executed
    assert any(e.get("resumed") for e in second.events)


async def test_budget_and_event_limits_stop_runs(env) -> None:  # type: ignore[no-untyped-def]
    goal = "Research the market for EV chargers"
    g = plan(compile_intent(goal), goal=goal)
    broke = await env["make"]().run(
        g, _ctx(env, goal, run_id="r_budget", budget_usd=Decimal("0.0000001"))
    )
    assert broke.status == "budget_exceeded"
    tiny = await env["make"](Limits(max_events=5)).run(g, _ctx(env, goal, run_id="r_events"))
    assert tiny.status == "partial" and "event limit" in tiny.warnings[0]


async def test_tool_retry_and_model_fallback(env) -> None:  # type: ignore[no-untyped-def]
    g = Graph(
        (
            Node("fetch", NodeKind.TOOL, {"tool": "flaky", "args": {}}, max_retries=1),
            Node("answer", NodeKind.LLM, {"tier": "small"}, ("fetch",)),
        ),
        "answer",
    )
    env["stub"].fail_next("small", ProviderErrorKind.OVERLOADED)
    result = await env["make"]().run(g, _ctx(env, "use the data", run_id="r_retry"))
    assert result.status == "completed"
    assert env["flaky"].calls == 2  # first attempt failed, retry succeeded
    assert result.nodes["answer"].model != "stub/small"  # fell back to the next model


async def test_human_approval_pauses(env) -> None:  # type: ignore[no-untyped-def]
    g = Graph(
        (
            Node("draft", NodeKind.LLM, {"tier": "small"}),
            Node("approve", NodeKind.HUMAN_APPROVAL, {}, ("draft",)),
            Node("send", NodeKind.LLM, {"tier": "small"}, ("approve",)),
        ),
        "send",
    )
    result = await env["make"]().run(g, _ctx(env, "draft a refund email", run_id="r_human"))
    assert result.status == "awaiting_approval" and "send" not in result.nodes
