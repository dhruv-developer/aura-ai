"""Prompt registry: templates, immutable versions, activation and rollback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aura_billing.application.entitlements import EntitlementService
from aura_cache import TieredCache
from aura_db import Database, audit
from aura_db.models import PromptTemplate, PromptVersion
from aura_shared.errors import Conflict, NotFound, ValidationFailed
from aura_shared.ids import new_id

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")


@dataclass(frozen=True, slots=True)
class ResolvedPrompt:
    template_id: str
    version_id: str
    version: int
    content: str


class PromptRegistry:
    def __init__(
        self, db: Database, *, cache: TieredCache, entitlements: EntitlementService
    ) -> None:
        self._db = db
        self._cache = cache
        self._ent = entitlements

    async def create(
        self, account_id: str, *, name: str, content: str, actor_id: str
    ) -> dict[str, str]:
        await self._ent.require(account_id, "prompt_optimization")
        if not _NAME.match(name):
            raise ValidationFailed("name must be lowercase letters, digits, '.', '_' or '-'")
        if not content.strip() or len(content) > 100_000:
            raise ValidationFailed("content must be 1-100000 characters")
        try:
            async with self._db.tenant(account_id) as s:
                tpl = PromptTemplate(id=new_id("prm"), account_id=account_id, name=name)
                s.add(tpl)
                await s.flush()
                ver = PromptVersion(
                    id=new_id("prmv"),
                    template_id=tpl.id,
                    account_id=account_id,
                    version=1,
                    content=content,
                    status="active",
                    created_by=actor_id,
                )
                s.add(ver)
                tpl.active_version_id = ver.id
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_id,
                    account_id=account_id,
                    action="prompt.create",
                    target_type="prompt",
                    target_id=tpl.id,
                )
                return {"template_id": tpl.id, "version_id": ver.id}
        except IntegrityError as exc:
            raise Conflict(f"prompt {name!r} already exists") from exc

    async def add_version(
        self,
        account_id: str,
        *,
        template_id: str,
        content: str,
        actor_id: str | None,
        origin: str = "manual",
        evaluation: dict[str, Any] | None = None,
    ) -> str:
        async with self._db.tenant(account_id) as s:
            tpl = await s.get(PromptTemplate, template_id, with_for_update=True)
            if tpl is None:
                raise NotFound("prompt not found")
            n = await s.scalar(
                select(func.coalesce(func.max(PromptVersion.version), 0)).where(
                    PromptVersion.template_id == template_id
                )
            )
            ver = PromptVersion(
                id=new_id("prmv"),
                template_id=template_id,
                account_id=account_id,
                version=int(n or 0) + 1,
                content=content,
                status="draft",
                origin=origin,
                evaluation=evaluation or {},
                created_by=actor_id,
            )
            s.add(ver)
            return ver.id

    async def activate(
        self, account_id: str, *, template_id: str, version_id: str, actor_id: str
    ) -> None:
        async with self._db.tenant(account_id) as s:
            tpl = await s.get(PromptTemplate, template_id, with_for_update=True)
            ver = await s.get(PromptVersion, version_id)
            if tpl is None or ver is None or ver.template_id != template_id:
                raise NotFound("prompt version not found")
            previous = tpl.active_version_id
            if previous and previous != version_id:
                old = await s.get(PromptVersion, previous)
                if old is not None:
                    old.status = "archived"
            ver.status = "active"
            tpl.active_version_id = version_id
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="prompt.activate",
                target_type="prompt_version",
                target_id=version_id,
                before={"active": previous},
                after={"active": version_id},
            )
            name = tpl.name
        await self._cache.invalidate(account_id, name)

    async def resolve(self, account_id: str, name: str) -> ResolvedPrompt:
        async def load() -> dict[str, Any] | None:
            async with self._db.tenant(account_id) as s:
                row = (
                    await s.execute(
                        select(PromptTemplate, PromptVersion)
                        .join(PromptVersion, PromptVersion.id == PromptTemplate.active_version_id)
                        .where(PromptTemplate.name == name)
                    )
                ).first()
            if row is None:
                return None
            tpl, ver = row
            return {"t": tpl.id, "v": ver.id, "n": ver.version, "c": ver.content}

        d = await self._cache.get_or_load(account_id, name, loader=load)
        if d is None:
            raise NotFound(f"prompt {name!r} not found")
        return ResolvedPrompt(d["t"], d["v"], d["n"], d["c"])

    async def list(self, account_id: str) -> list[dict[str, Any]]:
        async with self._db.tenant(account_id) as s:
            tpls = list(await s.scalars(select(PromptTemplate).order_by(PromptTemplate.name)))
            vers = list(await s.scalars(select(PromptVersion).order_by(PromptVersion.version)))
        return [
            {
                "id": t.id,
                "name": t.name,
                "active_version_id": t.active_version_id,
                "versions": [
                    {
                        "id": v.id,
                        "version": v.version,
                        "status": v.status,
                        "origin": v.origin,
                        "content": v.content,
                        "evaluation": v.evaluation,
                    }
                    for v in vers
                    if v.template_id == t.id
                ],
            }
            for t in tpls
        ]
