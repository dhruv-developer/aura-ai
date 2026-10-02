"""Event envelope. Every event is immutable, versioned and carries an idempotency id."""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from aura_shared.ids import new_id

NATS_PARTITIONS = 16


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Subclasses set these; consumers dispatch on (event_type, schema_version).
    event_type: ClassVar[str] = "event"
    schema_version: ClassVar[int] = 1

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    account_id: str
    region: str

    def envelope(self) -> dict[str, object]:
        return {
            "type": self.event_type,
            "schema_version": self.schema_version,
            "data": self.model_dump(mode="json"),
        }


def partition_for(account_id: str, partitions: int = NATS_PARTITIONS) -> int:
    """Stable tenant → partition mapping (crc32 is stable across processes, unlike hash())."""
    return zlib.crc32(account_id.encode()) % partitions


def subject_for(event: Event, *, stream: str = "exec") -> str:
    """NATS subject partitioned by region and tenant hash: ``aura.exec.in.p07.<type>``."""
    p = partition_for(event.account_id)
    return f"aura.{stream}.{event.region}.p{p:02d}.{event.event_type}"
