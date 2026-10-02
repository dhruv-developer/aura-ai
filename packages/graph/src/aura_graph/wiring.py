"""Build a graph executor from a provider stack (runtime and workers share this)."""

from __future__ import annotations

import httpx

from aura_graph.executor import GraphExecutor
from aura_graph.llm import CreditGate, CreditMeter, RoutedLlm
from aura_graph.tools import (
    BraveSearchTool,
    CalculatorTool,
    HttpFetchTool,
    RemoteSandboxTool,
    Tool,
    ToolRegistry,
)
from aura_memory import LongTermMemory, WorkingMemory
from aura_providers.domain.catalog import ModelCatalog
from aura_providers.registry import ProviderRegistry
from aura_routing import Router
from aura_shared.settings import Settings


def build_executor(
    settings: Settings,
    *,
    catalog: ModelCatalog,
    registry: ProviderRegistry,
    http: httpx.AsyncClient,
    memory: LongTermMemory | None,
    checkpoints: WorkingMemory | None,
    credit_gate: CreditGate | None = None,
    credit_meter: CreditMeter | None = None,
) -> tuple[GraphExecutor, bool]:
    """Returns (executor, can_search)."""
    tools: list[Tool] = [CalculatorTool(), HttpFetchTool(http)]
    can_search = False
    if settings.brave_api_key:
        tools.append(BraveSearchTool(http, settings.brave_api_key.get_secret_value()))
        can_search = True
    if settings.sandbox_url and settings.sandbox_api_key:
        tools.append(
            RemoteSandboxTool(
                http,
                base_url=settings.sandbox_url,
                api_key=settings.sandbox_api_key.get_secret_value(),
            )
        )
    llm = RoutedLlm(
        catalog=catalog,
        router=Router(catalog),
        registry=registry,
        credit_gate=credit_gate,
        credit_meter=credit_meter,
    )
    return GraphExecutor(
        llm=llm, tools=ToolRegistry(tools), memory=memory, checkpoints=checkpoints
    ), can_search
