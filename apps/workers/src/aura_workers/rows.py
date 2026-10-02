"""Event envelope → ClickHouse rows. Pure."""

from __future__ import annotations

import json
from typing import Any


def _ts(value: str) -> str:
    # ClickHouse DateTime64 accepts 'YYYY-MM-DD hh:mm:ss.fff'.
    return value.replace("T", " ").replace("Z", "").split("+")[0][:23]


def execution_rows(envelope: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    d = envelope["data"]
    calls = d.get("calls") or []
    served = next((c for c in reversed(calls) if not c.get("error")), None)
    routing = d.get("routing") or {}
    occurred = _ts(d["occurred_at"])
    execution = {
        "event_id": d["event_id"],
        "occurred_at": occurred,
        "account_id": d["account_id"],
        "region": d["region"],
        "workspace_id": d["workspace_id"],
        "environment_id": d.get("environment_id"),
        "workload_id": d.get("workload_id"),
        "policy_id": d.get("policy_id"),
        "trace_id": d["trace_id"],
        "execution_id": d["execution_id"],
        "session_id": d.get("session_id"),
        "mode": d.get("mode", "optimize"),
        "status": d["status"],
        "error_code": d.get("error_code"),
        "total_latency_ms": int(d["total_latency_ms"]),
        "total_cost_usd": str(d["total_cost"]),
        "model": served["model"] if served else "",
        "input_tokens": int(served["input_tokens"]) if served else 0,
        "output_tokens": int(served["output_tokens"]) if served else 0,
        "cached_input_tokens": int(served.get("cached_input_tokens", 0)) if served else 0,
        "attempts": len(calls),
        "router_version": routing.get("router_version", ""),
        "routing_reason": routing.get("reason", ""),
        "routing_features": json.dumps(routing.get("features") or {}, sort_keys=True),
        "propensity": float(routing.get("propensity", 1.0)),
        "notes": list(d.get("notes") or []),
    }
    call_rows = [
        {
            "event_id": d["event_id"],
            "occurred_at": occurred,
            "account_id": d["account_id"],
            "execution_id": d["execution_id"],
            "seq": i,
            "provider": c["provider"],
            "model": c["model"],
            "input_tokens": int(c["input_tokens"]),
            "output_tokens": int(c["output_tokens"]),
            "cached_input_tokens": int(c.get("cached_input_tokens", 0)),
            "latency_ms": int(c["latency_ms"]),
            "cost_usd": str(c["cost"]),
            "error": c.get("error"),
        }
        for i, c in enumerate(calls)
    ]
    return execution, call_rows
