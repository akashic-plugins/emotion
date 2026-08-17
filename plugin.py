from __future__ import annotations

import hashlib
import json
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

from agent.plugin_composition import (
    BACKGROUND_JOBS,
    BackgroundJobDefinition,
    Context,
    CoreEvent,
    CoreEventTrigger,
    MobileUiDefinition,
    MobileUiNavigation,
    MobileUiRpcInvalidRequest,
    PROACTIVE_COMPONENTS,
    ProactiveModuleDefinition,
    UI_SLOTS,
)
from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
from bus.events_lifecycle import DriftFinished, TurnCommitted
from proactive_v2.frame import ProactiveFrame

from .db import (
    apply_feedback,
    build_effect,
    commit_domain_effect,
    lookup_effect,
    lookup_domain_effect,
    lookup_domain_effect_path,
    open_db,
)
from .dashboard import EmotionDashboardReader

api_version = 3
name = "emotion"
version = "3.0.0"
desc = "Proactive VAD state and feedback preference projection."
inject = (BACKGROUND_JOBS, PROACTIVE_COMPONENTS, UI_SLOTS)
workspace_roots = ("emotion",)
drift_skill_roots = ("drift/skills",)
dashboard_module = "dashboard.py"
_v3_emotion_root: Path | None = None
_v3_emotion_module: "EmotionProjectionModule | None" = None

_FEEDBACK_CONTEXT_SKILL = "feedback-preference-context"
_FEEDBACK_PREVIEW_MAX_CHARS = 2400
_PROACTIVE_CONTEXT_TEMPLATE = """# Proactive Context

在这里写会影响未来主动推送取舍的稳定偏好。

- 主 agent 负责维护这份文件。
- proactive agent 每轮都会读取它作为额外上下文。
- 优先写短规则和倾向,避免写流程文档。
- 这里不提供新闻事实,不提供候选内容。
- 写结论即可,不要写冗长过程。
"""

_MERGE_PROACTIVE_CONTEXT_SYSTEM = (
    "你是 proactive context editor。"
    "你的职责是把候选反馈保守蒸馏成短规则，帮助未来主动推送更合适。"
    "不要把一次反馈扩写成策略文档，也不要制造新的硬约束。"
)

_MERGE_PROACTIVE_CONTEXT_PROMPT = """\
你的任务是把「待合并推送偏好候选」融合进「当前 Proactive Context」。

## 合并原则
- 只保留会稳定影响未来主动推送决策的偏好。
- 多条反馈支持同一方向时，可以写成规则；单条或弱证据只能写成轻量倾向。
- 把 pending 聚类成少量稳定主题；37 条候选也最多沉淀成 8 条新增或修改规则。
- pending 中 effect=boost/block/verify/timing/tone 分别对应提高优先级、降低/屏蔽、推送前核验、时机、表达方式。
- 合并同类项，删除重复、过窄、证据不足、no_candidate、临时状态和一次性事件。
- 优先修改现有相关 bullet，不要为轻量倾向新建 section。
- 保留当前 Proactive Context 中仍然有效的规则，但可以压缩被 pending 触及的冗长段落。

## 禁止事项
- 不写 evidence id、feedback id、message id、chunk、计数、推理过程或审核状态。
- 不写“数据来源”“触发条件”“执行注意”“计算逻辑”“白名单每周更新”这类流程说明。
- 不因为一条反馈就写“仅推送”“禁止”“一律过滤”“必须查询”这类硬规则。
- 不扩写成用户画像、聊天总结、新闻事实或配置文档。
- 不新增带“新增”“其他倾向”“通用规则”这类兜底标题的小节。

## 输出格式
- 直接输出完整 `# Proactive Context` markdown。
- 新增或修改的规则尽量一行一个 bullet，必要时最多两行。
- 全文长度不要超过当前 Proactive Context 的 1.25 倍。
- 优先使用“优先/降权/避免/保持/可适度”这类运行时上下文表达。
- 不要代码块，不要解释。

---
当前 Proactive Context：
{current_context}

待合并推送偏好候选：
{pending}
"""


async def apply(ctx: Context, config: object) -> None:
    """登记 Emotion 的 proactive module、typed Turn observer、job 与 UI。"""

    # 1. 只冻结 Core 投影的 generation-local 数据根，不打开数据库或调用模型。
    del config
    global _v3_emotion_module, _v3_emotion_root
    _v3_emotion_root = ctx.workspace_root("emotion")
    emotion_root = _v3_emotion_root
    _v3_emotion_module = EmotionProjectionModule(emotion_root)

    # 2. Proactive module 只在 formal domain-effect facade 中提交 SQLite 状态。
    await ctx.require(PROACTIVE_COMPONENTS).register(
        ctx,
        ProactiveModuleDefinition(
            slot="proactive.prompt.emotion",
            lifecycle_id="default.proactive.frame.v1",
            produces=(
                "proactive:prompt:system_bottom:emotion",
                "proactive:effect:emotion",
            ),
            handler_export="run_emotion_prompt_v3",
            domain_effect="emotion.state",
            domain_effect_lookup_export="lookup_emotion_domain_effect_v3",
        ),
    )

    # 3. JobHost 独占 event admission、LLM lease、effect receipt 与文档提交。
    await ctx.require(BACKGROUND_JOBS).register(
        ctx,
        BackgroundJobDefinition(
            name="merge_proactive_pending",
            triggers=(CoreEventTrigger(CoreEvent.DRIFT_FINISHED),),
            handler_export="merge_proactive_pending_v3",
            documents_scope=("emotion",),
            domain_effect="emotion.state",
            domain_effect_lookup_export="lookup_emotion_domain_effect_v3",
            model_role="agent",
        ),
    )

    # 4. TurnCommitted 是反馈唯一的 typed owner；listener 固定当前 Root。
    def on_turn_committed(event: TurnCommitted) -> None:
        _on_turn_committed(event, root=emotion_root)

    await ctx.on(AFTER_TURN_COMMITTED, on_turn_committed)

    # 5. Mobile 只读查询与静态资源绑定到同一个 generation Root。
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


class EmotionProjectionModule:
    """Build one proactive emotion projection and submit its domain receipt."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def run(self, context: object, frame: ProactiveFrame) -> ProactiveFrame:
        """Persist one formal frame effect through Core's exact domain facade."""

        # 1. Resolve the only allowed domain effect and derive stable frame inputs.
        effects = getattr(context, "domain_effects", None)
        if effects is None or not callable(getattr(effects, "run", None)):
            raise RuntimeError(
                "emotion proactive module 缺少 Core-owned domain effects facade"
            )
        session_key = str(
            frame.slots.get("proactive:session_key") or frame.input.session_key
        )
        base_threshold = float(
            frame.slots.get("proactive:base_judge_send_threshold") or 0.60
        )
        last_user_at = frame.slots.get("proactive:last_user_at")
        if last_user_at is not None and not hasattr(last_user_at, "tzinfo"):
            raise TypeError("emotion last_user_at 必须是 datetime 或 None")
        tick_id = f"{frame.input.session_key}:{frame.input.started_at.isoformat()}"
        result: dict[str, Any] = {}

        # 2. Core signs the effect only after this SQLite transaction has a durable receipt.
        def transaction(effect_context: object) -> None:
            exact_context = cast(Any, effect_context)
            conn = open_db(self._root / "emotion.db")
            try:
                conn.execute("BEGIN IMMEDIATE")
                if (
                    exact_context.event_id != tick_id
                    or exact_context.tick_id != tick_id
                ):
                    raise RuntimeError("Emotion proactive tick identity 与 Core 不一致")
                effect: dict[str, Any]
                result["effect"] = build_effect(
                    conn,
                    tick_id=tick_id,
                    session_key=session_key,
                    now_utc=frame.input.started_at,
                    last_user_at=cast(Any, last_user_at),
                    base_threshold=base_threshold,
                    commit=False,
                )
                effect = cast(dict[str, Any], result["effect"])
                _ = commit_domain_effect(
                    conn,
                    semantic_job_id=exact_context.semantic_job_id,
                    event_id=exact_context.event_id,
                    invocation_id=exact_context.invocation_id,
                    effect_id=exact_context.effect_id,
                    idempotency_key=exact_context.idempotency_key,
                    attempt=exact_context.attempt,
                    result_digest=_effect_digest(effect),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

        await effects.run("emotion.state", transaction)
        effect = result.get("effect")
        if not isinstance(effect, dict):
            conn = open_db(self._root / "emotion.db")
            try:
                effect = lookup_effect(conn, tick_id=tick_id)
            finally:
                conn.close()
        if not isinstance(effect, dict):
            raise RuntimeError("emotion domain effect 未返回 frame projection")
        frame.slots["proactive:prompt:system_bottom:emotion"] = str(
            effect.get("prompt_section") or ""
        )
        frame.slots["proactive:effect:emotion"] = effect
        return frame


def _effect_digest(effect: Mapping[str, object]) -> str:
    """Return the stable digest Core records for one committed projection."""

    payload = json.dumps(
        effect,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def run_emotion_prompt_v3(
    context: object,
    frame: ProactiveFrame,
) -> ProactiveFrame:
    """Run the exact-generation emotion proactive module."""

    module = _v3_emotion_module
    if module is None:
        raise RuntimeError("emotion v3 generation 尚未完成 apply")
    return await module.run(context, frame)


def _on_turn_committed(event: TurnCommitted, *, root: Path | None = None) -> None:
    """Project one typed committed Turn into Emotion's idempotent SQLite state."""

    feedback = _feedback_from_turn(event)
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


def _feedback_from_turn(event: TurnCommitted) -> dict[str, Any] | None:
    """Read an optional typed feedback result, with explicit quote as the local fallback."""

    raw = event.extra.get("proactive_feedback")
    if isinstance(raw, Mapping):
        feedback_type = raw.get("feedback_type")
        confidence = raw.get("confidence")
        if isinstance(feedback_type, str) and isinstance(confidence, str):
            source = raw.get("event_id") or event.turn_id or event.persisted_user_message_id
            if isinstance(source, str) and source:
                payload = {
                    "feedback_event_id": str(source),
                    "user_message_id": event.persisted_user_message_id,
                    "assistant_message_id": event.assistant_message_id,
                    "proactive_message_id": raw.get("proactive_message_id"),
                    "feedback_type": feedback_type,
                    "confidence": confidence,
                    "pua_score": raw.get("pua_score"),
                    "lag_seconds": raw.get("lag_seconds"),
                    "matched_by": raw.get("matched_by", "typed_turn"),
                    "candidate_count": raw.get("candidate_count"),
                    "pa_score": raw.get("pa_score"),
                    "reason": raw.get("reason", "typed_turn"),
                    "user_content_preview": _feedback_preview(
                        raw.get("user_content_preview")
                        or event.persisted_user_message
                        or event.input_message
                    ),
                    "assistant_content_preview": _feedback_preview(
                        raw.get("assistant_content_preview") or event.assistant_response
                    ),
                    "proactive_content_preview": _feedback_preview(
                        raw.get("proactive_content_preview")
                        or _quoted_proactive_text(event.input_message)
                    ),
                }
                return {
                    "source_event_id": f"proactive_feedback:{source}",
                    "feedback_type": feedback_type,
                    "confidence": confidence,
                    "payload": payload,
                }

    # A quote is already an explicit feedback signal carried by the committed user message.
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
        "source_event_id": f"proactive_feedback:{source}",
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


async def merge_proactive_pending_v3(ctx: Any) -> None:
    """形成 merge 内容，并经 Core receipt fence 发布两份 proactive 文档。"""

    # 1. 非目标 Drift completion 不读取文档、不调用模型。
    event = ctx.event
    if not isinstance(event, DriftFinished):
        return
    if event.skill_name != _FEEDBACK_CONTEXT_SKILL or event.status != "completed":
        return
    if ctx.documents is None or ctx.domain_effects is None:
        raise RuntimeError("emotion merge job 缺少 Core documents/domain effects")

    # 2. 从窄 port 读取脱离 bytes，生成新文档并先持久化完整 intent。
    expected, current = ctx.documents.read_pair()
    pending = current.pending.decode("utf-8").strip()
    if not pending or "- [ ]" not in pending:
        return
    current_context = current.context.decode("utf-8").strip()
    if not current_context:
        current_context = _PROACTIVE_CONTEXT_TEMPLATE.strip()
    prompt = _MERGE_PROACTIVE_CONTEXT_PROMPT.format(
        current_context=current_context,
        pending=pending,
    )
    merged = await ctx.llm.generate_text(
        system=_MERGE_PROACTIVE_CONTEXT_SYSTEM,
        prompt=prompt,
        max_tokens=4096,
    )
    if not merged:
        return
    pair = {
        "context": merged.strip().encode("utf-8") + b"\n",
        "pending": b"",
    }
    intent = await ctx.documents.prepare_pair(expected, pair)
    result_digest = hashlib.sha256(pair["context"] + b"\0" + pair["pending"]).hexdigest()

    # 3. Emotion SQLite 提交领域 receipt 后，Core 才能向前提交文档。
    async def transaction(effect_ctx: Any) -> None:
        db = open_db(_require_v3_emotion_root() / "emotion.db")
        try:
            _ = commit_domain_effect(
                db,
                semantic_job_id=effect_ctx.semantic_job_id,
                event_id=effect_ctx.event_id or event.event_id,
                invocation_id=effect_ctx.invocation_id,
                effect_id=effect_ctx.effect_id,
                idempotency_key=effect_ctx.idempotency_key,
                attempt=effect_ctx.attempt,
                result_digest=result_digest,
            )
        finally:
            db.close()

    receipt = await ctx.domain_effects.run("emotion.state", transaction)
    _ = await ctx.documents.commit_after(intent, receipt)


def lookup_emotion_domain_effect_v3(effect_ctx: Any) -> object | None:
    """只读返回 Emotion durable receipt，供 Core 重签 exact capability。"""

    return lookup_domain_effect_path(
        _require_v3_emotion_root() / "emotion.db",
        invocation_id=effect_ctx.invocation_id,
        effect_id=effect_ctx.effect_id,
        idempotency_key=effect_ctx.idempotency_key,
    )


def _require_v3_emotion_root() -> Path:
    if _v3_emotion_root is None:
        raise RuntimeError("emotion v3 generation 尚未绑定 workspace root")
    return _v3_emotion_root
