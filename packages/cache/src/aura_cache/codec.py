"""Value codecs for the distributed tier."""

from __future__ import annotations

import json
from typing import Any, Protocol


class Codec(Protocol):
    def dumps(self, value: Any) -> bytes: ...
    def loads(self, raw: bytes) -> Any: ...


class JsonCodec:
    def dumps(self, value: Any) -> bytes:
        return json.dumps(value, separators=(",", ":"), default=str).encode()

    def loads(self, raw: bytes) -> Any:
        return json.loads(raw)
