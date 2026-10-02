"""Self-contained, printable HTML rendering of an X-Ray report (print → PDF)."""

from __future__ import annotations

from html import escape
from typing import Any

_CSS = " ".join(
    [
        "body{font:14px/1.5 system-ui,-apple-system,sans-serif;color:#111;",
        "margin:40px auto;max-width:960px;padding:0 24px}",
        "h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px}",
        ".muted{color:#666}",
        ".kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}",
        ".kpi{border:1px solid #ddd;border-radius:8px;padding:12px}",
        ".kpi b{display:block;font-size:22px}",
        "table{border-collapse:collapse;width:100%}",
        "th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eee}",
        "th{font-weight:600;color:#444}td.n{text-align:right;font-variant-numeric:tabular-nums}",
        ".note{background:#f6f6f6;border-radius:8px;padding:12px;margin-top:24px}",
        "@media print{body{margin:0}.kpi{break-inside:avoid}}",
    ]
)


def _row(cells: list[Any], numeric: set[int]) -> str:
    return (
        "<tr>"
        + "".join(
            f'<td class="n">{escape(str(c))}</td>' if i in numeric else f"<td>{escape(str(c))}</td>"
            for i, c in enumerate(cells)
        )
        + "</tr>"
    )


def render_html(name: str, report: dict[str, Any]) -> str:
    s = report.get("summary", {})
    opps = report.get("opportunities", [])
    workloads = report.get("workloads", [])
    models = report.get("models", [])
    kpis = [
        ("Monthly AI spend", f"${s.get('monthly_spend_usd', '0')}"),
        ("Expected monthly savings", f"${s.get('est_monthly_savings_usd', '0')}"),
        ("Savings", f"{s.get('est_savings_pct', 0)}%"),
        ("Requests analysed", f"{s.get('requests', 0):,}"),
    ]
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>AURA X-Ray: {escape(name)}</title><style>{_CSS}</style></head><body>",
        f"<h1>AI Workload X-Ray</h1><div class='muted'>{escape(name)} · "
        f"{escape(str(s.get('window_start') or ''))} → "
        f"{escape(str(s.get('window_end') or ''))}</div>",
        "<div class='kpis'>",
        *(
            f"<div class='kpi'><span class='muted'>{escape(k)}</span><b>{escape(v)}</b></div>"
            for k, v in kpis
        ),
        "</div><h2>Optimization opportunities</h2><table><tr><th>Opportunity</th><th>Workload</th>"
        "<th>Requests</th><th>Expected $/mo</th><th>Potential $/mo</th><th>Confidence</th></tr>",
        *(
            _row(
                [
                    o["title"],
                    o["workload"],
                    o["affected_requests"],
                    o["expected_monthly_savings_usd"],
                    o["potential_monthly_savings_usd"],
                    f"{int(o['confidence'] * 100)}%",
                ],
                {2, 3, 4, 5},
            )
            for o in opps
        ),
        "</table><h2>Workloads</h2><table><tr><th>Workload</th><th>Requests</th><th>$/mo</th>"
        "<th>Expected savings $/mo</th><th>p50 input tokens</th><th>Models</th></tr>",
        *(
            _row(
                [
                    w["label"],
                    w["requests"],
                    w["monthly_spend_usd"],
                    w["est_monthly_savings_usd"],
                    w["input_tokens_p50"],
                    ", ".join(w["models"]),
                ],
                {1, 2, 3, 4},
            )
            for w in workloads
        ),
        "</table><h2>Models</h2><table><tr><th>Model</th><th>Tier</th><th>Requests</th>"
        "<th>$/mo</th><th>Share of spend</th></tr>",
        *(
            _row(
                [
                    m["model"],
                    m["tier"],
                    m["requests"],
                    m["monthly_spend_usd"],
                    f"{m['share_of_spend'] * 100:.1f}%",
                ],
                {2, 3, 4},
            )
            for m in models
        ),
        f"</table><div class='note'>{escape(str(s.get('methodology', '')))}"
        + (
            f" {s['unpriced_requests']} requests used models without known prices and are "
            "excluded from spend."
            if s.get("unpriced_requests")
            else ""
        )
        + "</div></body></html>",
    ]
    return "".join(parts)
