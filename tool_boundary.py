"""Emotion 与 tools owner 之间的最小结构边界。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from agent.plugin_composition import Context, Effect, ServiceKey
from agent.plugin_contracts import ContentPart


ToolOutcome = Literal["success", "denied", "error", "interrupted"]


@dataclass(frozen=True, slots=True)
class ToolResultValue:
    """tools owner 在执行边界重新校验的结构结果。"""

    outcome: ToolOutcome
    parts: tuple[ContentPart, ...]


class ToolCatalog(Protocol):
    """tools owner 的注册入口；Emotion 不持有注册表或执行器实现。"""

    async def declare_group(
        self,
        ctx: Context,
        *,
        always_on: bool = False,
        description: str = "未声明用途",
    ) -> Effect: ...

    async def register(
        self,
        ctx: Context,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        open: Callable[[Mapping[str, object]], AbstractAsyncContextManager[object]],
        capture: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        public: bool = True,
        idempotent: bool = False,
        risk: Literal["read-only", "read-write", "external-side-effect"] = "read-write",
        search_hint: str | None = None,
    ) -> object: ...


TOOLS = ServiceKey[ToolCatalog]("tools.v1")


__all__ = ["ToolCatalog", "ToolResultValue", "TOOLS"]
