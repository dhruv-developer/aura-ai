"""The prompt library: every template renders, variables are strict, migrated prompts
kept their exact wording, and AURA prompts don't creep back into Python code."""

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from aura_shared.prompts import prompt_id, render, render_with_id, template_names

ROOT = Path(__file__).resolve().parents[3]

# Representative variables for every template (a new template must be added here).
SAMPLE_VARS: dict[str, dict[str, object]] = {
    "agents/drafter_system.j2": {
        "root_question": "Q",
        "parent_question": "P",
        "history": "User: hi",
        "memory": ["reply in Hindi"],
    },
    "agents/drafter_task.j2": {"question": "q"},
    "agents/drafter_revise.j2": {"round": 1, "max_rounds": 2, "issues": ["fix x"]},
    "agents/reviewer_system.j2": {"max_follow_ups": 2},
    "agents/reviewer_user.j2": {
        "root_question": "Q",
        "parent_question": None,
        "question": "q",
        "draft": "d",
        "attempt": 1,
        "max_attempts": 3,
        "allow_follow_ups": True,
        "history": None,
    },
    "agents/reviewer_json_retry.j2": {},
    "agents/finaliser_system.j2": {},
    "agents/finaliser_user.j2": {
        "root_question": "Q",
        "history": None,
        "memory": [],
        "nodes": [
            {"question": "a", "answer": "x", "status": "approved", "issues": [], "children": []}
        ],
    },
    "evaluation/judge.j2": {"request": "r", "a": "A", "b": "B"},
    "graph/role_analyst.j2": {},
    "graph/role_assistant.j2": {},
    "graph/role_coder.j2": {},
    "graph/role_writer.j2": {},
    "graph/split.j2": {"max_subqueries": 5, "request": "r"},
    "graph/split_tasks.j2": {"max_subqueries": 5, "request": "r"},
    "graph/subquery_answer.j2": {},
    "graph/subquery_combine.j2": {"listing": "sq1: a"},
    "graph/subquery_task_answer.j2": {},
    "graph/subquery_task_combine.j2": {"listing": "sq1: a"},
    "graph/verify_system.j2": {},
    "graph/verify_user.j2": {"goal": "g", "sources": "", "answer": "a"},
    "memory/memory_block.j2": {"lines": ["- a", "- b"]},
    "memory/session_summary.j2": {"summary": "s"},
}


def test_every_template_renders_and_is_versioned() -> None:
    assert sorted(SAMPLE_VARS) == template_names()  # nothing untested, nothing stale
    for name, variables in SAMPLE_VARS.items():
        r = render_with_id(name, **variables)
        assert r.text.strip() and "{{" not in r.text and "{%" not in r.text
        assert r.prompt_id.endswith("@v1") and r.prompt_id == prompt_id(name)
        assert "\n\n\n" not in r.text


def test_missing_variables_fail_loudly() -> None:
    with pytest.raises(UndefinedError):
        render("graph/verify_user.j2", goal="g", sources="")  # no answer


def test_migrated_prompts_kept_their_exact_wording() -> None:
    judge = render("evaluation/judge.j2", request="R", a="X", b="Y")
    assert judge.startswith("You are an impartial evaluator. Compare two responses")
    assert "<request>\nR\n</request>\n\n<response_a>\nX\n</response_a>" in judge
    assert judge.endswith("Reply with exactly one word: A, B, or TIE.")
    assert render("graph/verify_user.j2", goal="G", sources="", answer="A") == (
        "Question: G\n\nSources:\n(none)\n\nAnswer:\nA\n\n"
        "Is the answer correct and supported? Reply SUPPORTED or UNSUPPORTED."
    )
    split_rendered = render("graph/split.j2", max_subqueries=5, request="R")
    assert "You are an objective query decomposition engine." in split_rendered
    assert "Request:\nR" in split_rendered
    combine_rendered = render("graph/subquery_combine.j2", listing="sq1: a")
    assert "You are an objective synthesis engine." in combine_rendered
    assert "sq1: a" in combine_rendered
    assert render("memory/session_summary.j2", summary="S") == "Summary of earlier conversation:\nS"
    assert render("memory/memory_block.j2", lines=["- a", "- b"]) == (
        "Relevant memory and knowledge (use it when it helps; it may be incomplete):\n- a\n- b"
    )


def test_no_aura_prompt_text_inline_in_python() -> None:
    """Guard: prompts live in templates/, so these phrases must not appear in code."""
    phrases = [
        "You are an impartial evaluator",
        "You check answers against sources",
        "You are a precise, helpful assistant",
        "Split the user's request into independent sub-questions",
        "Summary of earlier conversation",
        "Relevant memory and knowledge",
        "You are the Reviewer",
        "You are the Drafter",
        "You are the Finaliser",
    ]
    offenders = []
    for path in [*ROOT.glob("packages/*/src/**/*.py"), *ROOT.glob("apps/*/src/**/*.py")]:
        text = path.read_text("utf-8")
        offenders += [f"{path.relative_to(ROOT)}: {p}" for p in phrases if p in text]
    assert offenders == []
