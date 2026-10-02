"""AURA's own prompts, as Jinja2 templates in ``templates/`` (never strings in code).

- ``render(name, **vars)`` returns the text.
- ``render_with_id`` also returns ``prompt_id@version`` (from the template's
  ``{# id: … · vN #}`` header), recorded in traces and telemetry so a prompt change can be
  tied to quality and cost.

Missing variables raise immediately (``StrictUndefined``): a half-rendered prompt must
never reach a model. Output is plain text (no HTML autoescape). Customer-authored prompts
(the prompt registry) are user data and do not live here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

_BLANK_RUNS = re.compile(r"\n{3,}")
_HEADER = re.compile(r"\{#\s*id:\s*([\w.]+)\s*·\s*v(\d+)\s*#\}")


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    prompt_id: str  # e.g. "agents.reviewer@v1"


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=PackageLoader("aura_shared", "prompts/templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - prompts are plain text, never HTML
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )


@lru_cache(maxsize=256)
def prompt_id(name: str) -> str:
    env = _env()
    assert env.loader is not None
    source, _, _ = env.loader.get_source(env, name)
    m = _HEADER.search(source)
    if not m:
        raise ValueError(f"prompt template {name!r} has no '{{# id: … · vN #}}' header")
    return f"{m.group(1)}@v{m.group(2)}"


def render_with_id(name: str, **variables: Any) -> Rendered:
    text = _env().get_template(name).render(**variables).strip("\n")
    text = _BLANK_RUNS.sub("\n\n", text)  # layout from template control flow, not content
    return Rendered(text, prompt_id(name))


def render(name: str, **variables: Any) -> str:
    return render_with_id(name, **variables).text


def template_names() -> list[str]:
    return sorted(_env().list_templates(extensions=["j2"]))
