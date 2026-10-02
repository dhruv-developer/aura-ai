"""Text embeddings: a port plus two implementations.

- ``HashingEmbedder``: deterministic feature hashing of word unigrams and bigrams, L2-
  normalized. It needs no model and no network, and catches near-paraphrases with shared
  wording. It is the default for local runs and tests and a cheap lexical tier.
- ``OpenAICompatibleEmbedder``: any ``/embeddings`` endpoint (OpenAI, vLLM, Ollama,
  Voyage-compatible gateways) for real semantic similarity.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol

import httpx

from aura_providers.domain.types import ProviderError, ProviderErrorKind

_WORD = re.compile(r"\w+", re.UNICODE)


def _stem(word: str) -> str:
    """Very light English suffix stripping (refunds/refunded/refunding → refund)."""
    for suffix in ("ingly", "edly", "ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


class Embedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _vector(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        stems = [_stem(w) for w in words]
        feats = words + stems + [f"{a} {b}" for a, b in itertools.pairwise(stems)]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += -1.0 if (h >> 63) & 1 else 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class OpenAICompatibleEmbedder:
    def __init__(
        self, http: httpx.AsyncClient, *, base_url: str, api_key: str, model: str, dim: int
    ) -> None:
        self._http, self._url, self._key = http, base_url.rstrip("/") + "/embeddings", api_key
        self.name, self.dim = model, dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._http.post(
                self._url,
                json={"model": self.name, "input": texts},
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, "embeddings unavailable") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE, f"embeddings HTTP {resp.status_code}"
            )
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [list(map(float, d["embedding"])) for d in data]
