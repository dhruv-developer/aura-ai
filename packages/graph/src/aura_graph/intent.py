"""Intent compiler: goal → structured intent. Pure and explainable (keyword rules). The
logged intents give an LLM-based compiler supervised training data later."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class TaskType(StrEnum):
    CHAT = "chat"
    QA = "qa"  # answer from knowledge
    RESEARCH = "research"  # gather + analyze + verify + synthesize
    SUMMARIZE = "summarize"
    CODE = "code"
    MATH = "math"  # deterministic: no LLM needed


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RULES: list[tuple[TaskType, re.Pattern[str]]] = [
    (
        TaskType.MATH,
        re.compile(r"^\s*(what is|calculate|compute|evaluate)?\s*[-+*/().\d\s^%]+[?=]?\s*$", re.I),
    ),
    (
        TaskType.CODE,
        re.compile(
            r"\b(write|implement|fix|debug|refactor)\b.*\b(code|function|script|class|bug|sql|python)\b|```",
            re.I,
        ),
    ),
    (TaskType.SUMMARIZE, re.compile(r"\b(summari[sz]e|tl;?dr|key points|condense)\b", re.I)),
    (
        TaskType.RESEARCH,
        re.compile(
            r"\b(research|investigate|compare|analy[sz]e|market|landscape"
            r"|pros and cons|competitors?)\b",
            re.I,
        ),
    ),
]
_HIGH_RISK = re.compile(
    r"\b(medical|diagnos|legal|lawsuit|contract|financial advice|invest|dosage|tax)\w*", re.I
)
_MEDIUM_RISK = re.compile(r"\b(policy|refund|compliance|security|price|pricing|customer)\w*", re.I)


@dataclass(frozen=True, slots=True)
class Intent:
    task: TaskType
    risk: Risk
    needs: frozenset[str] = field(default_factory=frozenset)  # retrieval | search | code
    reason: str = ""


def compile_intent(goal: str, *, has_knowledge: bool = False, can_search: bool = False) -> Intent:
    task, reason = TaskType.CHAT, "default"
    for t, pattern in _RULES:
        if pattern.search(goal):
            task, reason = t, f"matched {t.value} cues"
            break
    if task is TaskType.CHAT and has_knowledge:
        task, reason = TaskType.QA, "knowledge base attached"
    risk = (
        Risk.HIGH
        if _HIGH_RISK.search(goal)
        else Risk.MEDIUM
        if _MEDIUM_RISK.search(goal)
        else Risk.LOW
    )
    needs: set[str] = set()
    if has_knowledge and task in (TaskType.QA, TaskType.RESEARCH, TaskType.SUMMARIZE):
        needs.add("retrieval")
    if can_search and task is TaskType.RESEARCH:
        needs.add("search")
    if task is TaskType.CODE:
        needs.add("code")
    return Intent(task, risk, frozenset(needs), reason)
