"""Log parsers → TrafficRecord. Pure. Parsers are tolerant: malformed rows are counted
and skipped, never fatal, because real exports are messy."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from aura_shared.errors import ValidationFailed
from aura_xray.domain.records import TrafficRecord

MAX_RECORDS = 200_000


@dataclass
class ParseResult:
    records: list[TrafficRecord] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)  # first few, for the user

    def skip(self, reason: str) -> None:
        self.skipped += 1
        if len(self.errors) < 10:
            self.errors.append(reason)


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
            n = float(value)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UTC)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _from_openai_obj(obj: dict[str, Any]) -> TrafficRecord:
    """Accepts either {request:{...}, response:{...}} log entries or flat objects."""
    req: dict[str, Any] = obj["request"] if isinstance(obj.get("request"), dict) else obj
    resp: dict[str, Any] = obj["response"] if isinstance(obj.get("response"), dict) else obj
    messages = req.get("messages") or []
    system = "\n".join(
        _text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")
    )
    user = "\n".join(_text(m.get("content")) for m in messages if m.get("role") == "user")
    usage = resp.get("usage") or obj.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    choice = (resp.get("choices") or [{}])[0] if resp.get("choices") else {}
    model = str(resp.get("model") or req.get("model") or "")
    if not model:
        raise ValueError("missing model")
    inp = usage.get("prompt_tokens", usage.get("input_tokens"))
    out = usage.get("completion_tokens", usage.get("output_tokens"))
    if inp is None or out is None:
        raise ValueError("missing usage")
    meta = obj.get("metadata") or req.get("metadata") or {}
    error = obj.get("error") or resp.get("error")
    return TrafficRecord(
        ts=_ts(obj.get("timestamp") or obj.get("created_at") or resp.get("created")),
        model=model,
        input_tokens=int(inp),
        output_tokens=int(out),
        cached_input_tokens=int(
            details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
        ),
        latency_ms=int(obj["latency_ms"]) if obj.get("latency_ms") is not None else None,
        cost_usd=_dec(obj.get("cost_usd") or obj.get("cost")),
        status="error" if error else "ok",
        finish_reason=choice.get("finish_reason"),
        system_prompt=system or None,
        user_content=user or None,
        workload_hint=(meta.get("workload") or obj.get("workload"))
        if isinstance(meta, dict)
        else None,
        response_text=_text((choice.get("message") or {}).get("content")) or None,
    )


def parse_jsonl(lines: Iterable[str]) -> ParseResult:
    result = ParseResult()
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if len(result.records) >= MAX_RECORDS:
            raise ValidationFailed(f"at most {MAX_RECORDS} records per X-Ray upload")
        try:
            result.records.append(_from_openai_obj(json.loads(line)))
        except (ValueError, TypeError, AttributeError) as exc:
            result.skip(f"line {n}: {exc}")
    return result


CSV_REQUIRED = {"model", "input_tokens", "output_tokens"}


def parse_csv(text: str) -> ParseResult:
    result = ParseResult()
    reader = csv.DictReader(io.StringIO(text))
    missing = CSV_REQUIRED - set(reader.fieldnames or [])
    if missing:
        raise ValidationFailed(f"CSV is missing columns: {', '.join(sorted(missing))}")
    for n, row in enumerate(reader, 2):
        if len(result.records) >= MAX_RECORDS:
            raise ValidationFailed(f"at most {MAX_RECORDS} records per X-Ray upload")
        try:
            result.records.append(
                TrafficRecord(
                    ts=_ts(row.get("timestamp")),
                    model=row["model"].strip(),
                    input_tokens=int(row["input_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                    cached_input_tokens=int(row.get("cached_input_tokens") or 0),
                    latency_ms=int(row["latency_ms"]) if row.get("latency_ms") else None,
                    cost_usd=_dec(row.get("cost_usd")),
                    status=(row.get("status") or "ok").strip() or "ok",
                    finish_reason=row.get("finish_reason") or None,
                    system_prompt=row.get("system_prompt") or None,
                    user_content=row.get("prompt") or row.get("user_content") or None,
                    workload_hint=row.get("workload") or None,
                    response_text=row.get("response") or None,
                )
            )
        except (ValueError, KeyError, AttributeError) as exc:
            result.skip(f"row {n}: {exc}")
    return result


def parse(data: bytes, fmt: str) -> ParseResult:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("upload must be UTF-8 text") from exc
    if fmt == "jsonl":
        return parse_jsonl(text.splitlines())
    if fmt == "csv":
        return parse_csv(text)
    raise ValidationFailed("format must be 'jsonl' or 'csv'")


def iter_lines(data: bytes) -> Iterator[str]:
    yield from data.decode("utf-8-sig").splitlines()
