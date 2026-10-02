"""Reject oversized request bodies before they are read (pure ASGI middleware)."""

from __future__ import annotations

import json
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        path_limits: list[tuple[str, int]] | None = None,
    ) -> None:
        """``path_limits``: (regex, max_bytes) pairs that raise the limit for specific
        routes (e.g. file uploads). First match wins; otherwise ``max_bytes``."""
        self.app = app
        self.max_bytes = max_bytes
        self._path_limits = [(re.compile(p), n) for p, n in (path_limits or [])]

    def _limit_for(self, path: str) -> int:
        for pattern, limit in self._path_limits:
            if pattern.fullmatch(path):
                return limit
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope.get("path", ""))
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send, limit)
            return
        # Also guard chunked bodies without a Content-Length.
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await self._reject(send, limit)

    async def _reject(self, send: Send, limit: int) -> None:
        body = json.dumps(
            {
                "type": "https://docs.aura.ai/errors/payload_too_large",
                "title": "Payload too large",
                "status": 413,
                "code": "payload_too_large",
                "detail": f"request body exceeds {limit} bytes",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/problem+json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass
