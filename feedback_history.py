from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from agent.control.timer import TimerHandle, TimerStatus
from agent.plugin_composition import Context, PluginTimers, ServiceKey

from .db import (
    apply_feedback_history_page,
    open_db,
    read_feedback_history_cursor,
)


_POLL_INTERVAL = timedelta(minutes=1)
_PAGE_SIZE = 50
_RETRYABLE_SQLITE_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_IOERR,
    }
)


class FeedbackHistoryRecord(Protocol):
    cursor: int
    event_id: str
    payload_hash: str
    session_key: str
    user_message_id: str
    assistant_message_id: str
    proactive_message_id: str | None
    feedback_type: str
    confidence: str
    pa_score: float | None
    pua_score: float | None
    lag_seconds: int | None
    candidate_count: int
    matched_by: str
    reason: str
    user_content_preview: str | None
    assistant_content_preview: str | None
    proactive_content_preview: str | None


class FeedbackHistoryPage(Protocol):
    after_cursor: int
    records: Sequence[FeedbackHistoryRecord]


class FeedbackHistory(Protocol):
    def page(self, *, after_cursor: int, max_items: int) -> FeedbackHistoryPage: ...


PROACTIVE_FEEDBACK_HISTORY = ServiceKey[FeedbackHistory](
    "proactive-feedback.history.v1"
)


class FeedbackHistoryConsumer:
    """Pull PF accepted history into Emotion through one ordinary Timer chain."""

    def __init__(
        self,
        ctx: Context,
        root: Path,
        timers: PluginTimers,
        history: FeedbackHistory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ctx = ctx
        self._db_path = root / "emotion.db"
        self._timers = timers
        self._history = history
        self._now = now
        self._runner: asyncio.Task[None] | None = None
        self._handle: TimerHandle | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Emotion PF history consumer 已关闭")
        if self._runner is not None:
            return
        self._runner = await self._ctx.spawn(
            self._run(),
            name="emotion-feedback-history",
        )

    async def close(self) -> None:
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

    def tick_once(self) -> bool:
        """Pull and atomically apply one page, returning whether it was full."""

        # 1. A missing Emotion DB begins at zero without creating state for an empty page.
        cursor = 0
        if self._db_path.exists():
            connection = open_db(self._db_path)
            try:
                cursor = read_feedback_history_cursor(connection)
            finally:
                connection.close()
        page = self._history.page(after_cursor=cursor, max_items=_PAGE_SIZE)
        if page.after_cursor != cursor:
            raise RuntimeError("PF history page after_cursor 与请求不一致")
        if not page.records:
            return False

        # 2. Convert the structural cross-plugin DTO at the trust boundary.
        records = [_record_payload(record) for record in page.records]
        connection = open_db(self._db_path)
        try:
            applied_cursor = apply_feedback_history_page(connection, records)
        finally:
            connection.close()
        if applied_cursor != records[-1]["cursor"]:
            raise RuntimeError("Emotion PF history cursor 未推进到页尾")
        return len(records) == _PAGE_SIZE

    async def _run(self) -> None:
        """Use Timer receipts for every pull and retry only explicit transient I/O."""

        deadline = self._aware_now()
        while not self._closed:
            handle = self._timers.schedule(deadline)
            self._handle = handle
            try:
                receipt = await handle.result()
            finally:
                await handle.cleanup()
                if self._handle is handle:
                    self._handle = None
            if receipt.status is TimerStatus.CANCELLED:
                continue
            try:
                full_page = self.tick_once()
            except OSError as error:
                self._report_transient(error)
                full_page = False
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", None) not in _RETRYABLE_SQLITE_CODES:
                    raise
                self._report_transient(error)
                full_page = False
            deadline = (
                self._aware_now()
                if full_page
                else self._aware_now() + _POLL_INTERVAL
            )

    def _report_transient(self, error: BaseException) -> None:
        _ = self._ctx.report_incident(
            "emotion_feedback_history_transient",
            f"Emotion feedback history 暂态失败: {error}",
        )

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Emotion feedback history clock 必须带时区")
        return value.astimezone(UTC)


def _record_payload(record: FeedbackHistoryRecord) -> dict[str, object]:
    return {
        field: getattr(record, field)
        for field in (
            "cursor",
            "event_id",
            "payload_hash",
            "session_key",
            "user_message_id",
            "assistant_message_id",
            "proactive_message_id",
            "feedback_type",
            "confidence",
            "pa_score",
            "pua_score",
            "lag_seconds",
            "candidate_count",
            "matched_by",
            "reason",
            "user_content_preview",
            "assistant_content_preview",
            "proactive_content_preview",
        )
    }
