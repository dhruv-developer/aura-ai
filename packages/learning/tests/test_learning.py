import random

import pytest

from aura_learning import FEATURE_DIM, LinUCB, LoggedDecision, estimate, feature_vector, reward

ARMS = ["small", "frontier"]


def world(rng: random.Random) -> tuple[list[float], dict[str, float]]:
    """Ground truth: the small model succeeds on simple tasks, the frontier model on hard
    ones. Rewards already include the cost penalty (frontier is pricier)."""
    complexity = rng.random()
    x = feature_vector({"complexity": complexity, "est_input_tokens": 500})
    p_small = 0.95 if complexity < 0.5 else 0.3
    p_frontier = 0.97
    return x, {"small": p_small - 0.05, "frontier": p_frontier - 0.35}


def test_linucb_learns_contextual_routing() -> None:
    rng = random.Random(1)
    bandit = LinUCB(FEATURE_DIM, alpha=0.5)
    for _ in range(3000):
        x, expected = world(rng)
        arm, p = bandit.choose(x, ARMS, epsilon=0.05, rng=rng)
        assert 0 < p <= 1
        bandit.update(arm, x, expected[arm] + rng.gauss(0, 0.05))
    easy = feature_vector({"complexity": 0.1, "est_input_tokens": 500})
    hard = feature_vector({"complexity": 0.95, "est_input_tokens": 500})
    assert max(ARMS, key=lambda a: bandit.ucb(a, easy)) == "small"
    assert max(ARMS, key=lambda a: bandit.ucb(a, hard)) == "frontier"
    probs = bandit.probabilities(easy, ARMS, epsilon=0.1)
    assert probs["small"] == pytest.approx(0.95) and sum(probs.values()) == pytest.approx(1.0)
    restored = LinUCB.from_dict(bandit.to_dict())
    assert restored.ucb("small", easy) == pytest.approx(bandit.ucb("small", easy))


def test_off_policy_evaluation_recovers_true_value() -> None:
    rng = random.Random(2)
    logs = []
    for _ in range(8000):  # logging policy: uniform random
        x, expected = world(rng)
        a = rng.choice(ARMS)
        logs.append(LoggedDecision(tuple(x), a, 0.5, expected[a] + rng.gauss(0, 0.05)))

    def oracle(x: tuple[float, ...]) -> dict[str, float]:  # the target policy to evaluate
        return {"small": 1.0, "frontier": 0.0} if x[1] < 0.5 else {"small": 0.0, "frontier": 1.0}

    true_value = 0.5 * (0.95 - 0.05) + 0.5 * (0.97 - 0.35)
    result = estimate(logs, oracle, bootstrap=200)
    for name in ("ips", "snips", "dr"):
        assert result["estimates"][name] == pytest.approx(true_value, abs=0.04), name
    lo, hi = result["ci95"]["dr"]
    # A 95% interval misses 1 time in 20, so a fixed-seed test allows a small margin.
    assert lo - 0.01 <= true_value <= hi + 0.01
    assert result["estimates"]["dr"] > result["logging_policy_value"]  # oracle beats random


def test_reward_shape() -> None:
    assert reward(success=True, cost_usd=0, cost_ref_usd=0.01) == 1.0
    cheap = reward(success=True, cost_usd=0.001, cost_ref_usd=0.01)
    pricey = reward(success=True, cost_usd=0.02, cost_ref_usd=0.01)
    assert cheap > pricey and reward(success=False, cost_usd=0.001, cost_ref_usd=0.01) < 0
    assert len(feature_vector({})) == FEATURE_DIM


def test_warm_start_prior() -> None:
    b = LinUCB(FEATURE_DIM)
    b.warm_start("small", mean_reward=0.9, pseudo_count=20)
    b.warm_start("frontier", mean_reward=0.4, pseudo_count=20)
    assert b.theta("small")[0] > b.theta("frontier")[0]
