from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from agent.plugin_composition import (
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    TIMERS,
    Context,
    Effect,
    MobileUiDefinition,
    MobileUiNavigation,
    MobileUiRpcInvalidRequest,
    ServiceKey,
    UI_SLOTS,
)
from agent.plugin_contracts import ContentPart, Message, json_value

from .db import apply_feedback, open_db
from .dashboard import EmotionDashboardReader
from .feedback_history import (
    PROACTIVE_FEEDBACK_HISTORY,
    FeedbackHistoryConsumer,
)
from .message_boundary import MESSAGE_CATALOG, TURN_PROJECTION
from .runtime import (
    CommittedTurn,
    DriftProposalServices,
    DriftWakeServices,
    EmotionRuntime,
    normalize_preference_arguments,
)
from .tool_boundary import TOOLS, ToolResultValue

api_version = 3
name = "emotion"
version = "3.0.4"
desc = "Timer-refreshed Emotion context and ordinary Drift preference projection."
DRIFT_PROPOSALS = ServiceKey[DriftProposalServices]("drift.proposals.v1")
DRIFT_WAKE = ServiceKey[DriftWakeServices]("drift.wake.v1")
workspace_roots = ("emotion",)
drift_skill_roots = ("drift/skills",)
dashboard_module = "dashboard.py"
web_module = "web_module.js"
web_requires = ("workbench.panels.v2",)
web_provides = ()
web_contract_digests = {
    "workbench.panels.v2": "fb6417c9bf532c1fdb344767d06065d5d3293da85deb64eff1e8088889a33bcb",
}
_v3_emotion_root: Path | None = None
_v3_emotion_runtime: EmotionRuntime | None = None

_FEEDBACK_PREVIEW_MAX_CHARS = 2400


class MaterialRegistry(Protocol):
    async def register(
        self,
        ctx: Context,
        *,
        name: str,
        prepare: Callable[[Sequence[Message], str], Awaitable[Mapping[str, object]]],
        priority: int = 0,
        prompt: bool = False,
        reduce: object | None = None,
    ) -> Effect: ...


MATERIALS = ServiceKey[MaterialRegistry]("context.materials.v3")
inject = (
    TIMERS,
    TOOLS,
    UI_SLOTS,
    MATERIALS,
    MESSAGE_CATALOG,
    TURN_PROJECTION,
    DRIFT_PROPOSALS,
    DRIFT_WAKE,
)


class _EmotionPreferenceTool:
    """Expose Emotion's real commit operation through the tools owner boundary."""

    idempotent = True

    def __init__(self, runtime: EmotionRuntime):
        self._runtime = runtime

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: object | None = None,
    ) -> Mapping[str, object] | str:
        """Validate the fixed schema once and return the final JSON arguments."""

        del source
        try:
            return normalize_preference_arguments(arguments)
        except (TypeError, ValueError) as error:
            return str(error)

    async def invoke(
        self, key: str, arguments: Mapping[str, object]
    ) -> ToolResultValue:
        """Commit through Emotion's durable owner and return its real receipt."""

        del key
        raw = json_value(arguments)
        if not isinstance(raw, Mapping):
            raise TypeError("工具参数必须是 object")
        result = await self._runtime.commit_preference_context(None, raw)
        return ToolResultValue(
            "success",
            (ContentPart("text", json.dumps(result, ensure_ascii=False, sort_keys=True)),),
        )

    async def query(self, key: str) -> ToolResultValue | None:
        """The durable Emotion commit is replay-safe; no separate query receipt exists."""

        del key
        return None


async def apply(ctx: Context, config: object) -> None:
    """Compose Emotion from Timer, materials, ordinary Tool, Drift, and UI atoms."""

    # 1. 冻结 exact Root；apply/candidate 不打开数据库、不登记真实 Timer。
    del config
    global _v3_emotion_root, _v3_emotion_runtime
    _v3_emotion_root = ctx.workspace_root("emotion")
    emotion_root = _v3_emotion_root
    def on_turn_committed(event: CommittedTurn) -> None:
        _on_turn_committed(event, root=emotion_root, runtime=runtime)

    runtime = EmotionRuntime(
        ctx,
        emotion_root,
        ctx.require(TIMERS),
        ctx.require(DRIFT_PROPOSALS),
        ctx.require(DRIFT_WAKE),
        catalog=ctx.require(MESSAGE_CATALOG),
        projection=ctx.require(TURN_PROJECTION),
        on_turn=on_turn_committed,
    )
    _v3_emotion_runtime = runtime

    # 2. Drift 结果只经 tools owner 的普通 provider 进入 Emotion 自有事务。
    catalog = ctx.require(TOOLS)
    _ = await catalog.declare_group(ctx, description=desc)

    @asynccontextmanager
    async def open_tool(_state: Mapping[str, object]):
        yield _EmotionPreferenceTool(runtime)

    _ = await catalog.register(
        ctx,
        name="emotion_commit_preference_context",
        description=(
            "提交当前 Emotion Drift proposal 的稳定主动偏好；"
            "完整结果进入 Emotion history，current context 原位替换。"
        ),
        parameters=_commit_tool_schema(),
        open=open_tool,
        idempotent=True,
        risk="read-write",
        search_hint="emotion drift preference feedback",
    )

    # 3. Wake current is a low-trust material, not a lifecycle/Core event.
    materials = ctx.require(MATERIALS)

    async def prepare_material(
        snapshot: Sequence[Message], source: str
    ) -> Mapping[str, object]:
        return runtime.prepare_material(snapshot, source)

    _ = await materials.register(
        ctx,
        name="emotion-current",
        prepare=prepare_material,
        priority=150,
    )

    # 4. Timer/lifecycle/Turn observer 都属于同一个 generation Fiber。
    await ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
    await ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())

    # 5. PF history is an optional pull composition with its own Timer/Fiber.
    async def apply_feedback_history(child: Context) -> None:
        consumer = FeedbackHistoryConsumer(
            child,
            emotion_root,
            child.require(TIMERS),
            child.require(PROACTIVE_FEEDBACK_HISTORY),
        )
        await child.on(RUNTIME_STARTED, lambda _: consumer.start())
        await child.on(RUNTIME_STOPPING, lambda _: consumer.close())

    _ = await ctx.inject(
        (TIMERS, PROACTIVE_FEEDBACK_HISTORY),
        apply_feedback_history,
        name="proactive-feedback-history",
    )

    # 6. Mobile 只读查询与静态资源绑定到同一个 generation Root。
    def mobile_query(
        method: str,
        payload: dict[str, object],
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> dict[str, object]:
        return _mobile_ui_query(
            method,
            payload,
            session_id=session_id,
            turn_id=turn_id,
            root=emotion_root,
        )

    await ctx.require(UI_SLOTS).register_mobile(
        ctx,
        MobileUiDefinition(
            module="mobile_panel.js",
            stylesheet="mobile_panel.css",
            navigation=MobileUiNavigation(
                label="主动状态",
                description="反馈如何改变 Agent 的语气和主动发送把握",
            ),
        ),
        query=mobile_query,
    )


def _on_turn_committed(
    event: CommittedTurn,
    *,
    root: Path | None = None,
    runtime: EmotionRuntime | None = None,
) -> None:
    """Project one typed committed Turn into Emotion's idempotent SQLite state."""

    if runtime is not None:
        runtime.observe_turn(event)
    feedback = _explicit_quote_feedback_from_turn(event)
    if feedback is None:
        return
    root = _require_v3_emotion_root() if root is None else root
    conn = open_db(root / "emotion.db")
    try:
        apply_feedback(
            conn,
            source_event_id=feedback["source_event_id"],
            session_key=event.session_key,
            feedback_type=feedback["feedback_type"],
            confidence=feedback["confidence"],
            payload=feedback["payload"],
        )
    finally:
        conn.close()


def _explicit_quote_feedback_from_turn(
    event: CommittedTurn,
) -> dict[str, Any] | None:
    """Project Emotion's direct explicit-quote signal in its own namespace."""

    marker = "【你当前新消息】"
    source = event.turn_id or event.persisted_user_message_id
    if marker not in event.input_message or not isinstance(source, str) or not source:
        return None
    payload = {
        "feedback_event_id": source,
        "user_message_id": event.persisted_user_message_id,
        "assistant_message_id": event.assistant_message_id,
        "proactive_message_id": None,
        "feedback_type": "explicit_quote",
        "confidence": "gold",
        "pua_score": 1.0,
        "lag_seconds": None,
        "matched_by": "explicit_quote",
        "candidate_count": None,
        "pa_score": None,
        "reason": "explicit_quote",
        "user_content_preview": _feedback_preview(
            event.persisted_user_message or event.input_message
        ),
        "assistant_content_preview": _feedback_preview(event.assistant_response),
        "proactive_content_preview": _feedback_preview(
            _quoted_proactive_text(event.input_message)
        ),
    }
    return {
        "source_event_id": f"emotion_explicit_quote:{source}",
        "feedback_type": "explicit_quote",
        "confidence": "gold",
        "payload": payload,
    }


def _feedback_preview(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    if len(clean) <= _FEEDBACK_PREVIEW_MAX_CHARS:
        return clean
    return clean[:_FEEDBACK_PREVIEW_MAX_CHARS].rstrip() + "..."


def _quoted_proactive_text(value: str) -> str | None:
    marker = "【你当前新消息】"
    quoted = value.split(marker, 1)[0].strip()
    prefix = "被回复消息："
    if quoted.startswith(prefix):
        quoted = quoted[len(prefix) :].strip()
    return quoted or None


def _mobile_ui_query(
    method: str,
    payload: dict[str, object],
    *,
    session_id: str | None,
    turn_id: str | None,
    root: Path | None = None,
) -> dict[str, object]:
    """Return a read-only bounded Emotion mobile projection."""

    _ = session_id, turn_id
    if method != "emotion.bootstrap":
        raise MobileUiRpcInvalidRequest(f"未知 emotion 移动方法: {method}")
    limit = payload.get("limit", 30)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise MobileUiRpcInvalidRequest("limit 必须是 1 到 50 的整数")
    emotion_root = _require_v3_emotion_root() if root is None else root
    return EmotionDashboardReader(emotion_root).get_mobile_bootstrap(
        limit=limit
    )


async def emotion_commit_preference_context(
    context: object,
    arguments: Mapping[str, object],
) -> str:
    """Delegate legacy export lookup to the exact Root runtime."""

    runtime = _v3_emotion_runtime
    if runtime is None:
        raise RuntimeError("emotion v3 generation 尚未完成 apply")
    result = await runtime.commit_preference_context(context, arguments)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _commit_tool_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string"},
            "revision": {"type": "string"},
            "context": {
                "type": "string",
                "description": "完整且简短的当前主动偏好上下文。",
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "effect": {
                            "type": "string",
                            "enum": ["block", "boost", "timing", "tone", "verify"],
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "low", "medium"],
                        },
                        "topic": {"type": "string"},
                        "action": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                        },
                    },
                    "required": [
                        "effect",
                        "confidence",
                        "topic",
                        "action",
                        "evidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["proposal_id", "revision", "context", "candidates"],
        "additionalProperties": False,
    }


def _require_v3_emotion_root() -> Path:
    if _v3_emotion_root is None:
        raise RuntimeError("emotion v3 generation 尚未绑定 workspace root")
    return _v3_emotion_root
