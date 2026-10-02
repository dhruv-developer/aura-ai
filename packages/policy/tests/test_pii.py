from dataclasses import dataclass

import pytest

from aura_policy import ExecutionPolicy, PiiMode, from_dict, merge, to_dict
from aura_policy.pii import PiiBlocked, apply_pii_policy, detect, luhn_ok, redact, verhoeff_ok


@dataclass(frozen=True)
class Msg:
    role: str
    content: str


@dataclass(frozen=True)
class Req:
    messages: tuple[Msg, ...]


def test_checksums() -> None:
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert verhoeff_ok("234123412346") and not verhoeff_ok("234123412345")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("write to priya.s@example.co.in today", "EMAIL"),
        ("call +91 98765 43210", "PHONE"),
        ("card 4111-1111-1111-1111", "CARD"),
        ("aadhaar 2341 2341 2346", "AADHAAR"),
        ("PAN ABCDE1234F", "PAN"),
        ("key sk-abcdefghijklmnopqrstuvwxyz", "SECRET"),
    ],
)
def test_detects(text: str, kind: str) -> None:
    assert [f.kind for f in detect(text)] == [kind]


@pytest.mark.parametrize(
    "text",
    ["order 1234 5678 9012", "invoice 4111111111111112", "year 2026, 3 items", "ABCDE12345"],
)
def test_precision_on_ordinary_numbers(text: str) -> None:
    assert detect(text) == []


def test_redact_keeps_surrounding_text() -> None:
    out, kinds = redact("mail a@b.io and call 9876543210 please")
    assert out == "mail [EMAIL] and call [PHONE] please" and kinds == ["EMAIL", "PHONE"]


def test_apply_policy_modes() -> None:
    req = Req((Msg("system", "be nice"), Msg("user", "my email is a@b.io")))
    assert apply_pii_policy(req, PiiMode.ALLOW) == (req, [])
    red, notes = apply_pii_policy(req, PiiMode.REDACT)
    assert red.messages[1].content == "my email is [EMAIL]" and red.messages[0] is req.messages[0]
    assert notes == ["pii: redacted 1 items (email)"] and "a@b.io" not in notes[0]
    with pytest.raises(PiiBlocked) as exc:
        apply_pii_policy(req, PiiMode.BLOCK)
    assert exc.value.status == 422 and "a@b.io" not in str(exc.value)
    clean = Req((Msg("user", "hello"),))
    assert apply_pii_policy(clean, PiiMode.BLOCK) == (clean, [])


def test_pii_merge_is_stricter_wins_and_serializes() -> None:
    org = ExecutionPolicy(pii=PiiMode.BLOCK)
    assert merge(org, ExecutionPolicy(pii=PiiMode.ALLOW)).pii is PiiMode.BLOCK
    assert merge(None, ExecutionPolicy()).effective_pii is PiiMode.ALLOW
    assert from_dict(to_dict(ExecutionPolicy(pii=PiiMode.REDACT))).pii is PiiMode.REDACT
