"""DNS TXT lookups over DNS-over-HTTPS (no system resolver or extra dependency)."""

from __future__ import annotations

from typing import Protocol

import httpx

from aura_shared.errors import DependencyUnavailable


class TxtResolver(Protocol):
    async def txt(self, name: str) -> list[str]: ...


class DohTxtResolver:
    def __init__(
        self, http: httpx.AsyncClient, endpoint: str = "https://cloudflare-dns.com/dns-query"
    ) -> None:
        self._http, self._endpoint = http, endpoint

    async def txt(self, name: str) -> list[str]:
        resp = await self._http.get(
            self._endpoint,
            params={"name": name, "type": "TXT"},
            headers={"accept": "application/dns-json"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise DependencyUnavailable("DNS lookup failed")
        answers = resp.json().get("Answer") or []
        # TXT data arrives quoted, possibly split into several quoted strings.
        return [
            "".join(part for part in str(a.get("data", "")).split('"') if part.strip())
            for a in answers
            if a.get("type") == 16
        ]
