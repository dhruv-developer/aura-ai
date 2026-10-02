"""AURA evaluation."""

from aura_evaluation.comparison import (
    CandidateSummary,
    GateDecision,
    SampleEvaluation,
    evaluate_sample,
    gate,
    summarize,
)
from aura_evaluation.judge import JudgeVerdict, PairwiseJudge, TextCompleter, compare
from aura_evaluation.metrics import is_json, rule_check, token_f1
from aura_evaluation.stats import bootstrap_ci, wilson_interval

__all__ = [
    "CandidateSummary",
    "GateDecision",
    "JudgeVerdict",
    "PairwiseJudge",
    "SampleEvaluation",
    "TextCompleter",
    "bootstrap_ci",
    "compare",
    "evaluate_sample",
    "gate",
    "is_json",
    "rule_check",
    "summarize",
    "token_f1",
    "wilson_interval",
]
