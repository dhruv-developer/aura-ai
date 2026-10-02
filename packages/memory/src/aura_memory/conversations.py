"""Durable, encrypted chat conversations (the app's multiple chat sessions).

Session memory (Redis, 24 h) serves API callers that pass a ``session_id``. The app
needs more: a list of past chats that survives restarts, can be renamed and deleted,
and replays its history into each new turn. Each title and message is sealed with
envelope encryption, and the AAD binds it to its account, conversation and row, so a
ciphertext moved anywhere else fails to decrypt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from aura_db import Database
from aura_db.models import Conversation, ConversationMessage
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import NotFound, ValidationFailed
from aura_shared.ids import new_id

MODES = frozenset({"auto", "fast", "balanced", "best"})
TITLE_MAX = 80


def _aad(account_id: str, conversation_id: str, row: str) -> str:
    return f"{account_id}|conv|{conversation_id}|{row}"


def default_title(first_message: str) -> str:
    """No extra model call just to name a chat: the first line, trimmed."""
    line = " ".join(first_message.split())
    return (line[: TITLE_MAX - 1] + "…") if len(line) > TITLE_MAX else (line or "New chat")


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: str
    title: str
    mode: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MessageView:
    id: str
    role: str
    content: str
    aura: dict[str, Any] | None
    created_at: datetime
    trace: dict[str, Any] | None = None  # decrypted; how the answer was made


class ConversationStore:
    def __init__(self, db: Database, envelope: Envelope) -> None:
        self._db = db
        self._env = envelope

    def _seal(self, obj: Conversation | ConversationMessage, text: str, aad: str) -> None:
        s = self._env.seal(text, aad=aad)
        obj.key_id, obj.wrapped_dek, obj.nonce, obj.ciphertext = (
            s.key_id,
            s.wrapped_dek,
            s.nonce,
            s.ciphertext,
        )

    def _open(self, obj: Conversation | ConversationMessage, aad: str) -> str:
        return self._env.open(
            Sealed(obj.key_id, obj.wrapped_dek, obj.nonce, obj.ciphertext), aad=aad
        )

    def _view(self, c: Conversation) -> ConversationView:
        title = self._open(c, _aad(c.account_id, c.id, "title"))
        return ConversationView(c.id, title, c.mode, c.created_at, c.updated_at)

    @staticmethod
    def _mode(mode: str) -> str:
        if mode not in MODES:
            raise ValidationFailed(f"mode must be one of {sorted(MODES)}")
        return mode

    async def create(
        self, account_id: str, user_id: str, *, title: str = "New chat", mode: str = "auto"
    ) -> ConversationView:
        conv = Conversation(
            id=new_id("conv"), account_id=account_id, user_id=user_id, mode=self._mode(mode)
        )
        self._seal(
            conv, title.strip()[:TITLE_MAX] or "New chat", _aad(account_id, conv.id, "title")
        )
        async with self._db.tenant(account_id) as s:
            s.add(conv)
        return self._view(conv)

    async def _owned(self, s: Any, account_id: str, user_id: str, conv_id: str) -> Conversation:
        conv: Conversation | None = await s.get(Conversation, conv_id)
        # Another member's chat in a shared account is "not found", never "forbidden".
        if conv is None or conv.user_id != user_id or conv.account_id != account_id:
            raise NotFound("conversation not found")
        return conv

    async def list_for(
        self, account_id: str, user_id: str, *, limit: int = 100
    ) -> list[ConversationView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .limit(min(max(limit, 1), 500))
            )
            return [self._view(c) for c in rows]

    async def get(self, account_id: str, user_id: str, conv_id: str) -> ConversationView:
        async with self._db.tenant(account_id) as s:
            return self._view(await self._owned(s, account_id, user_id, conv_id))

    async def update(
        self,
        account_id: str,
        user_id: str,
        conv_id: str,
        *,
        title: str | None = None,
        mode: str | None = None,
    ) -> ConversationView:
        async with self._db.tenant(account_id) as s:
            conv = await self._owned(s, account_id, user_id, conv_id)
            if title is not None:
                if not title.strip():
                    raise ValidationFailed("title must not be empty")
                self._seal(conv, title.strip()[:TITLE_MAX], _aad(account_id, conv.id, "title"))
            if mode is not None:
                conv.mode = self._mode(mode)
            conv.updated_at = datetime.now(UTC)
            return self._view(conv)

    async def delete(self, account_id: str, user_id: str, conv_id: str) -> None:
        """Hard delete: the conversation and every message in it."""
        async with self._db.tenant(account_id) as s:
            conv = await self._owned(s, account_id, user_id, conv_id)
            await s.execute(
                delete(ConversationMessage).where(ConversationMessage.conversation_id == conv.id)
            )
            await s.delete(conv)

    async def messages(
        self, account_id: str, user_id: str, conv_id: str, *, limit: int = 500
    ) -> list[MessageView]:
        async with self._db.tenant(account_id) as s:
            await self._owned(s, account_id, user_id, conv_id)
            rows = list(
                await s.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == conv_id)
                    .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                    .limit(min(max(limit, 1), 2000))
                )
            )
        rows.reverse()
        return [
            MessageView(
                m.id,
                m.role,
                self._open(m, _aad(account_id, conv_id, m.id)),
                m.aura,
                m.created_at,
                self._open_trace(m, account_id, conv_id),
            )
            for m in rows
        ]

    def _open_trace(
        self, m: ConversationMessage, account_id: str, conv_id: str
    ) -> dict[str, Any] | None:
        if m.trace_ciphertext is None:
            return None
        sealed = Sealed(
            m.trace_key_id or "",
            m.trace_wrapped_dek or b"",
            m.trace_nonce or b"",
            m.trace_ciphertext,
        )
        data: dict[str, Any] = json.loads(
            self._env.open(sealed, aad=_aad(account_id, conv_id, f"{m.id}|trace"))
        )
        return data

    async def append(
        self,
        account_id: str,
        user_id: str,
        conv_id: str,
        *,
        role: str,
        content: str,
        aura: dict[str, Any] | None = None,
        retitle_from: str | None = None,
        trace: dict[str, Any] | None = None,
    ) -> MessageView:
        """Add a message and bump the conversation. ``retitle_from`` names a chat that
        still has the default title after its first message."""
        if role not in ("user", "assistant"):
            raise ValidationFailed("role must be user or assistant")
        msg = ConversationMessage(
            id=new_id("msg"), account_id=account_id, conversation_id=conv_id, role=role, aura=aura
        )
        self._seal(msg, content, _aad(account_id, conv_id, msg.id))
        if trace is not None:
            t = self._env.seal(json.dumps(trace), aad=_aad(account_id, conv_id, f"{msg.id}|trace"))
            msg.trace_key_id, msg.trace_wrapped_dek = t.key_id, t.wrapped_dek
            msg.trace_nonce, msg.trace_ciphertext = t.nonce, t.ciphertext
        async with self._db.tenant(account_id) as s:
            conv = await self._owned(s, account_id, user_id, conv_id)
            s.add(msg)
            conv.updated_at = datetime.now(UTC)
            if (
                retitle_from is not None
                and self._open(conv, _aad(account_id, conv.id, "title")) == "New chat"
            ):
                self._seal(conv, default_title(retitle_from), _aad(account_id, conv.id, "title"))
        return MessageView(msg.id, role, content, aura, msg.created_at, trace)
