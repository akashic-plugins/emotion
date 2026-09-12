"""Emotion 对消息目录和 Turn 投影的最小结构边界。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol

from agent.plugin_composition import ServiceKey
from agent.plugin_contracts import Message


class MessageReader(Protocol):
    def snapshot(
        self, *, after_seq: int = -1, through_seq: int | None = None
    ) -> tuple[Message, ...]: ...


class MessageCatalog(Protocol):
    def snapshot_attributes(self) -> Mapping[str, object]: ...

    def reader(self, session_id: str) -> MessageReader: ...

    def follow(self) -> AsyncIterator[Mapping[str, int]]: ...


class ProjectedTurn(Protocol):
    status: str
    ending_message_id: str | None
    message_ids: tuple[str, ...]


class TurnProjection(Protocol):
    def project(
        self, messages: Sequence[Message], source: str
    ) -> tuple[ProjectedTurn, ...]: ...


MESSAGE_CATALOG = ServiceKey[MessageCatalog]("core.message_catalog")
TURN_PROJECTION = ServiceKey[TurnProjection]("turn.projection.v1")


__all__ = ["MESSAGE_CATALOG", "TURN_PROJECTION", "MessageCatalog", "TurnProjection"]
