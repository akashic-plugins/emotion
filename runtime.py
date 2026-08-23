from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from agent.control.timer import TimerHandle, TimerStatus
from agent.lifecycle.types import BeforeTurnCtx
from agent.plugin_composition import Context, PluginTimers
from bus.events_lifecycle import TurnCommitted

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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ctx = ctx
        self._db_path = root / "emotion.db"
        self._timers = timers
        self._proposals = proposals
        self._drift = drift
        self._now = now
        self._runner: asyncio.Task[None] | None = None
        self._handle: TimerHandle | None = None
        self._closed = False

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

    async def close(self) -> None:
        """Cancel the owned Timer/task without changing durable Emotion facts."""

        self._closed = True
        handle = self._handle
        runner = self._runner
        self._handle = None
        self._runner = None
        if handle is not None:
            _ = await handle.cancel()
        if runner is not None and runner is not asyncio.current_task():
            _ = await asyncio.gather(runner, return_exceptions=True)
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
        _ = self._proposals.propose(
            proposal_id,
            revision,
            payload,
            now,
            next_due=now + _REFRESH_INTERVAL,
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

    def prepare_context(self, ctx: BeforeTurnCtx) -> None:
        """Append only a fresh current hint to Wake Turns."""

        if ctx.channel != "wake":
            return
        prompt = read_fresh_context(
            self._db_path,
            now=ctx.timestamp,
            max_age_seconds=_FRESH_SECONDS,
        )
        if prompt is not None:
            ctx.extra_hints.append("Emotion current:\n" + prompt)

    def observe_turn(self, event: TurnCommitted) -> None:
        """Record user activity and the selected ordinary Drift terminal."""

        # 1. Only committed non-Wake user messages update the current presence input.
        if event.channel != "wake" and event.persisted_user_message is not None:
            at = event.timestamp or self._aware_now()
            db = open_db(self._db_path)
            try:
                record_user_activity(db, at)
            finally:
                db.close()

        # 2. TurnCommitted runs before Wake settles selection, so the selected ref is readable.
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

    async def commit_preference_context(
        self,
        _tool_context: object,
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Validate and atomically commit one Emotion-owned Drift result."""

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


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} 必须是无首尾空白的非空字符串")
    return value


def _candidates(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("candidates 必须是 array")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("candidate 必须是 object")
        effect = _text(item.get("effect"), "candidate.effect")
        confidence = _text(item.get("confidence"), "candidate.confidence")
        if effect not in _CANDIDATE_EFFECTS:
            raise ValueError(f"未知 candidate.effect: {effect}")
        if confidence not in _CANDIDATE_CONFIDENCE:
            raise ValueError(f"未知 candidate.confidence: {confidence}")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or any(
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
