from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from agent.control.timer import TimerHandle, TimerStatus
from agent.plugin_composition import Context, PluginTimers
from agent.plugin_contracts import ContentPart, Input, Message, Output, json_value
from .db import (
    commit_drift_result,
    mark_drift_proposal_submitted,
    open_db,
    prepare_drift_proposal,
    read_fresh_context,
    record_drift_turn,
    record_user_activity,
    refresh_current_context,
)
from .message_boundary import MessageCatalog, TurnProjection


_REFRESH_INTERVAL = timedelta(minutes=5)
_FRESH_SECONDS = int(_REFRESH_INTERVAL.total_seconds() * 2)
_CANDIDATE_EFFECTS = frozenset({"boost", "block", "verify", "timing", "tone"})
_CANDIDATE_CONFIDENCE = frozenset({"low", "medium", "high"})


class DriftProposalServices(Protocol):
    def propose(
        self,
        proposal_id: str,
        revision: str,
        payload: Mapping[str, object],
        due_at: datetime,
        *,
        next_due: datetime | None = None,
    ) -> Mapping[str, object]: ...


class DriftWakeServices(Protocol):
    def selection(
        self, accepted_turn: Mapping[str, object]
    ) -> Mapping[str, object] | None: ...


class CommittedTurn(Protocol):
    """The small committed-turn view retained for direct unit fixtures."""

    session_key: str
    channel: str
    input_message: str
    persisted_user_message: str | None
    assistant_response: str
    turn_id: str | None
    persisted_user_message_id: str
    assistant_message_id: str
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class ObservedTurn:
    """One complete Turn reconstructed from the durable Message prefix."""

    session_key: str
    channel: str
    chat_id: str
    input_message: str
    persisted_user_message: str
    assistant_response: str
    tools_used: tuple[str, ...]
    turn_id: str
    persisted_user_message_id: str
    assistant_message_id: str
    timestamp: datetime


class EmotionRuntime:
    """Refresh current context and bridge Emotion evidence into ordinary Drift."""

    def __init__(
        self,
        ctx: Context,
        root: Path,
        timers: PluginTimers,
        proposals: DriftProposalServices,
        drift: DriftWakeServices,
        *,
        catalog: MessageCatalog | None = None,
        projection: TurnProjection | None = None,
        on_turn: Callable[[ObservedTurn], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ctx = ctx
        self._db_path = root / "emotion.db"
        self._timers = timers
        self._proposals = proposals
        self._drift = drift
        if (catalog is None) != (projection is None):
            raise ValueError("Emotion Message catalog 与 Turn projection 必须同时提供")
        self._catalog = catalog
        self._projection = projection
        self._on_turn = on_turn
        self._now = now
        self._runner: asyncio.Task[None] | None = None
        self._message_runner: asyncio.Task[None] | None = None
        self._handle: TimerHandle | None = None
        self._closed = False
        self._message_heads: dict[str, int] = {}
        self._observed_turns: set[tuple[str, str, str]] = set()

    async def start(self) -> None:
        """Start one Fiber-owned immediate tick and recurring one-shot chain."""

        if self._closed:
            raise RuntimeError("Emotion runtime 已关闭")
        if self._runner is not None:
            return
        self._runner = await self._ctx.spawn(
            self._run(),
            name="emotion-current-refresh",
        )
        if self._catalog is not None and self._projection is not None:
            self._message_runner = await self._ctx.spawn(
                self._follow_messages(),
                name="emotion-message-follow",
            )

    async def close(self) -> None:
        """Cancel the owned Timer/task without changing durable Emotion facts."""

        self._closed = True
        handle = self._handle
        runner = self._runner
        message_runner = self._message_runner
        self._handle = None
        self._runner = None
        self._message_runner = None
        if handle is not None:
            _ = await handle.cancel()
        if runner is not None and runner is not asyncio.current_task():
            _ = await asyncio.gather(runner, return_exceptions=True)
        if message_runner is not None and message_runner is not asyncio.current_task():
            message_runner.cancel()
            _ = await asyncio.gather(message_runner, return_exceptions=True)
        if handle is not None:
            await handle.cleanup()

    async def tick_once(self) -> None:
        """Refresh the singleton, then idempotently submit one pending proposal."""

        now = self._aware_now()
        db = open_db(self._db_path)
        try:
            _ = refresh_current_context(db, now=now)
            proposal = prepare_drift_proposal(db, now=now)
        finally:
            db.close()
        if proposal is None:
            return
        proposal_id = cast(str, proposal["proposal_id"])
        revision = cast(str, proposal["revision"])
        payload = cast(Mapping[str, object], proposal["payload"])
        due_at = cast(datetime, proposal["due_at"])
        _ = self._proposals.propose(
            proposal_id,
            revision,
            payload,
            due_at,
            next_due=due_at + _REFRESH_INTERVAL,
        )
        db = open_db(self._db_path)
        try:
            mark_drift_proposal_submitted(
                db,
                proposal_id=proposal_id,
                revision=revision,
            )
        finally:
            db.close()

    def prepare_material(
        self, snapshot: Sequence[Message], source: str
    ) -> Mapping[str, object]:
        """Return one fresh low-trust reminder for a Wake material request."""

        if source != "wake" or not snapshot:
            return {}
        prompt = read_fresh_context(
            self._db_path,
            now=snapshot[-1].recorded_at,
            max_age_seconds=_FRESH_SECONDS,
        )
        if prompt is None:
            return {}
        return {
            "reminders": (
                {
                    "name": "emotion-current",
                    "text": "Emotion current:\n" + prompt,
                    "priority": 150,
                },
            ),
        }

    def observe_turn(self, event: CommittedTurn) -> None:
        """Record user activity and the selected ordinary Drift terminal."""

        # 1. Only committed non-Wake user messages update the current presence input.
        if event.channel != "wake" and event.persisted_user_message is not None:
            at = event.timestamp or self._aware_now()
            db = open_db(self._db_path)
            try:
                record_user_activity(db, at)
            finally:
                db.close()

        # 2. The completed Wake Turn is observed before its selected ref expires.
        if event.channel != "wake" or not event.turn_id:
            return
        selected = self._drift.selection(
            {"session_id": event.session_key, "turn_id": event.turn_id}
        )
        if selected is None:
            return
        payload = selected.get("payload")
        ref = selected.get("ref")
        if not isinstance(payload, Mapping) or payload.get("owner") != "emotion":
            return
        if not isinstance(ref, Mapping):
            raise RuntimeError("Emotion Drift selection 缺少 ref")
        db = open_db(self._db_path)
        try:
            _ = record_drift_turn(
                db,
                proposal_id=_text(ref.get("proposal_id"), "proposal_id"),
                revision=_text(ref.get("revision"), "revision"),
                session_id=event.session_key,
                turn_id=event.turn_id,
                completed_at=event.timestamp or self._aware_now(),
            )
        finally:
            db.close()

    async def _follow_messages(self) -> None:
        """Follow committed Message heads and observe each complete projected Turn."""

        catalog = self._catalog
        projection = self._projection
        if catalog is None or projection is None:
            raise RuntimeError("Emotion Message follower 缺少 catalog 或 projection")
        async for heads in catalog.follow():
            for session_id, head in heads.items():
                if not isinstance(session_id, str) or type(head) is not int:
                    raise ValueError("Message head identity 无效")
                if head <= self._message_heads.get(session_id, -1):
                    continue
                messages = catalog.reader(session_id).snapshot(through_seq=head)
                self._observe_prefix(session_id, messages, projection)
                self._message_heads[session_id] = head

    def _observe_prefix(
        self,
        session_id: str,
        messages: tuple[Message, ...],
        projection: TurnProjection,
    ) -> None:
        """Project complete source Turns and invoke the plugin-owned observer once."""

        by_id = {message.message_id: message for message in messages}
        sources = tuple(
            sorted(
                {
                    message.source
                    for message in messages
                    if isinstance(message.body, Input)
                }
            )
        )
        for source in sources:
            for turn in projection.project(messages, source):
                ending_id = turn.ending_message_id
                if turn.status != "complete" or ending_id is None:
                    continue
                identity = (session_id, source, ending_id)
                if identity in self._observed_turns:
                    continue
                members = tuple(
                    by_id[message_id]
                    for message_id in turn.message_ids
                    if message_id in by_id
                )
                users = tuple(
                    message for message in members if isinstance(message.body, Input)
                )
                assistant = by_id.get(ending_id)
                if not users or assistant is None or not isinstance(assistant.body, Output):
                    self._observed_turns.add(identity)
                    continue
                observed = ObservedTurn(
                    session_key=session_id,
                    channel=source,
                    chat_id=session_id,
                    input_message="\n\n".join(_body_text(user.body.parts) for user in users),
                    persisted_user_message=_body_text(users[-1].body.parts),
                    assistant_response=_body_text(
                        tuple(
                            part
                            for part in assistant.body.parts
                            if isinstance(part, ContentPart)
                        )
                    ),
                    tools_used=(),
                    turn_id=ending_id,
                    persisted_user_message_id=users[-1].message_id,
                    assistant_message_id=assistant.message_id,
                    timestamp=assistant.recorded_at,
                )
                self._observed_turns.add(identity)
                if self._on_turn is not None:
                    self._on_turn(observed)

    async def commit_preference_context(
        self,
        _tool_context: object,
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Validate and atomically commit one Emotion-owned Drift result."""

        decoded = json_value(arguments)
        if not isinstance(decoded, Mapping):
            raise TypeError("工具参数必须是 object")
        arguments = decoded
        proposal_id = _text(arguments.get("proposal_id"), "proposal_id")
        revision = _text(arguments.get("revision"), "revision")
        context = _text(arguments.get("context"), "context")
        candidates = _candidates(arguments.get("candidates"))
        result: Mapping[str, object] = {
            "context": context,
            "candidates": candidates,
        }
        db = open_db(self._db_path)
        try:
            return commit_drift_result(
                db,
                proposal_id=proposal_id,
                revision=revision,
                result=result,
            )
        finally:
            db.close()

    async def _run(self) -> None:
        """Retry only explicit transient OS failures on the next ordinary tick."""

        while not self._closed:
            try:
                await self.tick_once()
            except OSError as exc:
                _ = self._ctx.report_incident(
                    "emotion_tick_transient",
                    f"Emotion refresh/proposal 暂态失败: {exc}",
                )
            if self._closed:
                return
            handle = self._timers.schedule(self._aware_now() + _REFRESH_INTERVAL)
            self._handle = handle
            try:
                receipt = await handle.result()
            finally:
                await handle.cleanup()
                if self._handle is handle:
                    self._handle = None
            if receipt.status is TimerStatus.CANCELLED:
                continue

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("Emotion clock 必须带时区")
        return now.astimezone(UTC)


def _body_text(parts: Sequence[ContentPart]) -> str:
    """Extract text blocks for Emotion's bounded feedback preview."""

    values = [
        part.value
        for part in parts
        if part.kind == "text" and isinstance(part.value, str)
    ]
    return "\n".join(values)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} 必须是无首尾空白的非空字符串")
    return value


def _candidates(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("candidates 必须是 array")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("candidate 必须是 object")
        if set(item) != {"effect", "confidence", "topic", "action", "evidence"}:
            raise ValueError("candidate 字段无效")
        effect = _text(item.get("effect"), "candidate.effect")
        confidence = _text(item.get("confidence"), "candidate.confidence")
        if effect not in _CANDIDATE_EFFECTS:
            raise ValueError(f"未知 candidate.effect: {effect}")
        if confidence not in _CANDIDATE_CONFIDENCE:
            raise ValueError(f"未知 candidate.confidence: {confidence}")
        evidence = item.get("evidence")
        if not isinstance(evidence, (list, tuple)) or any(
            type(sample_id) is not int or sample_id <= 0 for sample_id in evidence
        ):
            raise ValueError("candidate.evidence 必须是正整数 array")
        result.append(
            {
                "effect": effect,
                "confidence": confidence,
                "topic": _text(item.get("topic"), "candidate.topic"),
                "action": _text(item.get("action"), "candidate.action"),
                "evidence": list(evidence),
            }
        )
    return result


def normalize_preference_arguments(value: object) -> dict[str, object]:
    """Normalize one tools.v1 preference payload before Emotion commits it."""

    decoded = json_value(value)
    if not isinstance(decoded, Mapping):
        raise TypeError("工具参数必须是 object")
    if set(decoded) != {"proposal_id", "revision", "context", "candidates"}:
        raise ValueError("工具参数字段无效")
    return {
        "proposal_id": _text(decoded["proposal_id"], "proposal_id"),
        "revision": _text(decoded["revision"], "revision"),
        "context": _text(decoded["context"], "context"),
        "candidates": _candidates(decoded["candidates"]),
    }
