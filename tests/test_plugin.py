from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib.util
import inspect
import json
import sqlite3
import sys
import warnings
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent.control.timer import TimerReceipt, TimerStatus
from agent.lifecycle.types import BeforeTurnCtx
from agent.plugin_composition import (
    TIMERS,
    TOOL_CATALOG,
    UI_SLOTS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    RuntimeStarted,
    RuntimeStopping,
)
from agent.plugin_composition.tool_catalog import (
    PluginToolCatalog,
    PluginTools,
    _freeze_plugin_tools,
)
from agent.plugin_composition.ui_slots import PluginUiSlots
from bus.events_lifecycle import TurnCommitted
from plugins.drift.store import DriftStore

NOW = datetime(2026, 8, 23, 8, tzinfo=UTC)


def _load_plugin_module():
    path = Path(__file__).parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "test_emotion_plugin",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_plugin_module()
refresh_current_context = sys.modules[
    "test_emotion_plugin.db"
].refresh_current_context


def test_tool_export_matches_core_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    async def commit(
        context: object,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        assert context is tool_context
        assert arguments == {"proposal_id": "proposal-1"}
        return {"committed": True}

    tool_context = object()
    monkeypatch.setattr(
        module,
        "_v3_emotion_runtime",
        SimpleNamespace(commit_preference_context=commit),
    )
    handler = module.emotion_commit_preference_context
    assert tuple(inspect.signature(handler).parameters) == ("context", "arguments")
    assert json.loads(
        asyncio.run(handler(tool_context, {"proposal_id": "proposal-1"}))
    ) == {"committed": True}


class DriftServices:
    """Expose both ordinary Drift ports over the real Core store."""

    def __init__(self, path: Path) -> None:
        self.store = DriftStore(path)
        self.store.initialize()

    def propose(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self.store.propose(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    def selection(self, accepted_turn: dict[str, object]) -> dict[str, object] | None:
        return self.store.selection(accepted_turn)


class EmptyDrift:
    def propose(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"inserted": True}

    def selection(self, accepted_turn: object) -> None:
        return None


async def _mount_candidate(
    tmp_path: Path,
) -> tuple[CompositionRoot, Path, PluginToolCatalog]:
    """Mount the real plugin with candidate Timer and ordinary service atoms."""

    root = CompositionRoot("emotion-candidate")
    tools = PluginTools(root.instance_token)
    ui = PluginUiSlots()
    drift = EmptyDrift()
    _ = await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    _ = await root.context.provide(TOOL_CATALOG, tools)
    _ = await root.context.provide(UI_SLOTS, ui)
    _ = await root.context.provide(module.DRIFT_PROPOSALS, drift)
    _ = await root.context.provide(module.DRIFT_WAKE, drift)
    emotion_root = tmp_path / "workspace" / "emotion"
    _ = await root.mount(
        lambda ctx: module.apply(ctx, object()),
        name="emotion",
        inject=module.inject,
        runtime=PluginRuntime(
            plugin_id="emotion",
            plugin_dir=Path(__file__).parents[1],
            data_dir=tmp_path / "plugin-data",
            workspace=emotion_root.parent,
            config=None,
            workspace_roots=("emotion",),
            data_access="read_only",
        ),
    )
    catalog = _freeze_plugin_tools(
        tools,
        root.instance_token,
        {"emotion": root.generation_id},
    )
    return root, emotion_root, catalog


def _feedback_turn(turn_id: str = "turn-feedback-1") -> TurnCommitted:
    return TurnCommitted(
        session_key="mobile:test",
        channel="test",
        chat_id="chat",
        input_message="被回复消息：主动提醒某个主题\n\n【你当前新消息】继续这个主题",
        persisted_user_message="被回复消息：主动提醒某个主题\n\n【你当前新消息】继续这个主题",
        assistant_response="继续回答",
        tools_used=[],
        turn_id=turn_id,
        persisted_user_message_id="u1",
        assistant_message_id="a1",
        timestamp=NOW,
    )


def _before_turn(channel: str, at: datetime) -> BeforeTurnCtx:
    return BeforeTurnCtx(
        session_key="wake:default",
        channel=channel,
        chat_id="chat",
        content="tick",
        timestamp=at,
        retrieved_memory_block="",
        retrieval_trace_raw=None,
        history_messages=(),
    )


def test_module_uses_only_ordinary_v3_atoms() -> None:
    assert module.api_version == 3
    assert inspect.signature(module.apply).parameters.keys() == {"ctx", "config"}
    assert module.inject == (
        TIMERS,
        TOOL_CATALOG,
        UI_SLOTS,
        module.DRIFT_PROPOSALS,
        module.DRIFT_WAKE,
    )
    source = "\n".join(
        (Path(__file__).parents[1] / name).read_text(encoding="utf-8")
        for name in ("plugin.py", "runtime.py")
    )
    for removed in (
        "PROACTIVE_COMPONENTS",
        "BACKGROUND_JOBS",
        "DRIFT_FINISHED",
        "ProactiveModule",
        "ProactiveDocuments",
        'event.extra.get("proactive_feedback")',
    ):
        assert removed not in source
    db_source = (Path(__file__).parents[1] / "db.py").read_text(encoding="utf-8")
    for removed_write in (
        "class EmotionDomainEffect",
        "def build_effect(",
        "def commit_domain_effect(",
        "def lookup_domain_effect(",
        "def lookup_domain_effect_path(",
        "INSERT INTO emotion_effects",
        "INSERT INTO emotion_domain_effects",
    ):
        assert removed_write not in db_source


@pytest.mark.asyncio
async def test_candidate_mount_has_zero_timer_and_zero_workspace_write(tmp_path: Path) -> None:
    root, emotion_root, catalog = await _mount_candidate(tmp_path)
    binding = catalog.get("emotion_commit_preference_context")
    assert binding is not None
    assert binding.handler is module.emotion_commit_preference_context
    assert not emotion_root.exists()
    assert not list(tmp_path.rglob("*.db"))
    await root.dispose()


def test_feedback_history_is_idempotent_and_mobile_is_read_only(tmp_path: Path) -> None:
    emotion_root = tmp_path / "emotion"
    event = _feedback_turn()
    module._on_turn_committed(event, root=emotion_root)
    module._on_turn_committed(event, root=emotion_root)
    before = (emotion_root / "emotion.db").read_bytes()
    projection = module._mobile_ui_query(
        "emotion.bootstrap",
        {"limit": 10},
        session_id=None,
        turn_id=None,
        root=emotion_root,
    )
    after = (emotion_root / "emotion.db").read_bytes()
    assert projection["overview"]["event_count"] == 1
    assert projection["overview"]["influence_count"] == 1
    assert before == after


def test_open_db_closes_every_read_only_validation_connection(tmp_path: Path) -> None:
    path = tmp_path / "emotion.db"
    created = module.open_db(path)
    created.close()
    _ = gc.collect()

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always", ResourceWarning)
        for _ in range(4):
            connection = module.open_db(path)
            connection.close()
        _ = gc.collect()

    assert [warning for warning in seen if warning.category is ResourceWarning] == []


@pytest.mark.asyncio
async def test_empty_tick_overwrites_current_without_appending_history(tmp_path: Path) -> None:
    drift = EmptyDrift()
    runtime = module.EmotionRuntime(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        drift,
        drift,
        now=lambda: NOW,
    )
    await runtime.tick_once()
    await runtime.tick_once()

    conn = sqlite3.connect(tmp_path / "emotion.db")
    assert conn.execute("SELECT count(*) FROM emotion_context_current").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM emotion_drift_runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM emotion_events").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM emotion_feedback_samples").fetchone()[0] == 0
    conn.close()


def _create_formal_legacy_fixture(path: Path) -> None:
    """Create the exact original three-table formal schema without new migration code."""

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE emotion_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            valence REAL NOT NULL,
            arousal REAL NOT NULL,
            dominance REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE emotion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_plugin TEXT NOT NULL,
            source_event_id TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL,
            session_key TEXT NOT NULL,
            valence_before REAL NOT NULL,
            arousal_before REAL NOT NULL,
            dominance_before REAL NOT NULL,
            valence_delta REAL NOT NULL,
            arousal_delta REAL NOT NULL,
            dominance_delta REAL NOT NULL,
            valence_after REAL NOT NULL,
            arousal_after REAL NOT NULL,
            dominance_after REAL NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE emotion_effects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            tick_id TEXT NOT NULL UNIQUE,
            session_key TEXT NOT NULL,
            valence REAL NOT NULL,
            arousal REAL NOT NULL,
            dominance REAL NOT NULL,
            base_threshold REAL NOT NULL,
            final_threshold REAL NOT NULL,
            threshold_delta REAL NOT NULL,
            tone_label TEXT NOT NULL,
            expected_effect TEXT NOT NULL,
            prompt_section TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        INSERT INTO emotion_state VALUES(1, 0.2, -0.1, 0.3, '2026-08-20T00:00:00+00:00');
        """
    )
    for index in range(3):
        conn.execute(
            """
            INSERT INTO emotion_events(
                source_plugin, source_event_id, source_type, session_key,
                valence_before, arousal_before, dominance_before,
                valence_delta, arousal_delta, dominance_delta,
                valence_after, arousal_after, dominance_after, reason, payload_json
            ) VALUES('legacy', ?, 'feedback', 'legacy', 0, 0, 0,
                     0.1, 0, 0.1, 0.1, 0, 0.1, 'legacy', '{}')
            """,
            (f"legacy-event-{index}",),
        )
    for index in range(5):
        conn.execute(
            """
            INSERT INTO emotion_effects(
                tick_id, session_key, valence, arousal, dominance,
                base_threshold, final_threshold, threshold_delta,
                tone_label, expected_effect, prompt_section, metadata_json
            ) VALUES(?, 'legacy', 0, 0, 0, 0.6, 0.6, 0,
                     '平静', 'frozen', 'legacy prompt', '{}')
            """,
            (f"legacy-tick-{index}",),
        )
    conn.commit()
    conn.close()


def _insert_legacy_pf_event(path: Path, *, user_message_id: str = "u1") -> None:
    payload = {
        "feedback_event_id": "1",
        "user_message_id": user_message_id,
        "assistant_message_id": "a1",
        "proactive_message_id": "p1",
        "feedback_type": "topic_follow",
        "confidence": "high",
        "pa_score": 0.8,
        "pua_score": 0.7,
        "lag_seconds": 1,
        "candidate_count": 1,
        "matched_by": "pua",
        "reason": "fixture",
        "user_content_preview": "继续",
        "assistant_content_preview": "回答",
        "proactive_content_preview": "提醒",
    }
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            INSERT INTO emotion_events(
                source_plugin, source_event_id, source_type, session_key,
                valence_before, arousal_before, dominance_before,
                valence_delta, arousal_delta, dominance_delta,
                valence_after, arousal_after, dominance_after, reason, payload_json
            ) VALUES(
                'proactive_feedback', 'proactive_feedback:1', 'topic_follow',
                'mobile:test', 0.18, -0.1, 0.25, 0.02, 0, 0.05,
                0.2, -0.1, 0.3, 'topic_follow_high', ?
            )
            """,
            (json.dumps(payload, ensure_ascii=False),),
        )
        connection.commit()


def test_formal_legacy_upgrade_is_atomic_and_preserves_all_rows(tmp_path: Path) -> None:
    path = tmp_path / "emotion.db"
    _create_formal_legacy_fixture(path)
    upgraded = module.open_db(path)

    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 2
    assert upgraded.execute("SELECT count(*) FROM emotion_events").fetchone()[0] == 3
    assert upgraded.execute("SELECT count(*) FROM emotion_effects").fetchone()[0] == 5
    assert tuple(upgraded.execute(
        "SELECT valence, arousal, dominance FROM emotion_state WHERE id=1"
    ).fetchone()) == (0.2, -0.1, 0.3)
    assert upgraded.execute("SELECT count(*) FROM emotion_context_current").fetchone()[0] == 0
    assert upgraded.execute("SELECT count(*) FROM emotion_preference_state").fetchone()[0] == 1
    assert upgraded.execute("SELECT count(*) FROM emotion_domain_effects").fetchone()[0] == 0
    upgraded.close()


def test_malformed_legacy_fails_before_ddl_and_can_retry_after_repair(tmp_path: Path) -> None:
    path = tmp_path / "emotion.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE emotion_state(value TEXT)")
    conn.execute("INSERT INTO emotion_state VALUES('unmodified')")
    conn.commit()
    conn.close()
    original_bytes = path.read_bytes()

    with pytest.raises(RuntimeError, match="Emotion table schema 不匹配: emotion_state"):
        _ = module.open_db(path)
    assert path.read_bytes() == original_bytes
    check = sqlite3.connect(path)
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall() == [("emotion_state",)]
    assert check.execute("SELECT value FROM emotion_state").fetchall() == [
        ("unmodified",)
    ]
    check.execute("DROP TABLE emotion_state")
    check.commit()
    check.close()

    _create_formal_legacy_fixture(path)
    repaired = module.open_db(path)
    assert repaired.execute("PRAGMA user_version").fetchone()[0] == 2
    assert repaired.execute("SELECT count(*) FROM emotion_events").fetchone()[0] == 3
    repaired.close()


def test_removed_legacy_write_api_cannot_mutate_frozen_tables(tmp_path: Path) -> None:
    path = tmp_path / "emotion.db"
    _create_formal_legacy_fixture(path)
    conn = module.open_db(path)
    before = (
        conn.execute("SELECT count(*) FROM emotion_effects").fetchone()[0],
        conn.execute("SELECT count(*) FROM emotion_domain_effects").fetchone()[0],
    )
    conn.close()

    db_module = sys.modules["test_emotion_plugin.db"]
    for removed in (
        "EmotionDomainEffect",
        "build_effect",
        "commit_domain_effect",
        "lookup_domain_effect",
        "lookup_domain_effect_path",
    ):
        assert not hasattr(db_module, removed)

    reopened = module.open_db(path)
    after = (
        reopened.execute("SELECT count(*) FROM emotion_effects").fetchone()[0],
        reopened.execute("SELECT count(*) FROM emotion_domain_effects").fetchone()[0],
    )
    reopened.close()
    assert after == before == (5, 0)


@pytest.mark.asyncio
async def test_fresh_context_appends_only_to_wake(tmp_path: Path) -> None:
    drift = EmptyDrift()
    runtime = module.EmotionRuntime(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        drift,
        drift,
        now=lambda: NOW,
    )
    db = module.open_db(tmp_path / "emotion.db")
    refresh_current_context(db, now=NOW)
    db.close()

    wake = _before_turn("wake", NOW + timedelta(minutes=1))
    wake.extra_hints.append("existing")
    runtime.prepare_context(wake)
    assert wake.extra_hints[0] == "existing"
    assert wake.extra_hints[1].startswith("Emotion current:\n")
    assert wake.abort is False

    passive = _before_turn("mobile", NOW + timedelta(minutes=1))
    runtime.prepare_context(passive)
    assert passive.extra_hints == []
    stale = _before_turn("wake", NOW + timedelta(minutes=11))
    runtime.prepare_context(stale)
    assert stale.extra_hints == []


@pytest.mark.asyncio
async def test_real_drift_replays_then_revises_and_commits_without_history_loss(
    tmp_path: Path,
) -> None:
    emotion_root = tmp_path / "emotion"
    module._on_turn_committed(_feedback_turn(), root=emotion_root)
    drift = DriftServices(tmp_path / "drift.sqlite3")
    runtime = module.EmotionRuntime(
        cast(Any, object()),
        emotion_root,
        PluginTimers.candidate_validation(),
        drift,
        drift,
        now=lambda: NOW,
    )

    await runtime.tick_once()
    await runtime.tick_once()
    snapshot = cast(
        tuple[dict[str, Any], ...],
        drift.store.snapshot(NOW)["proposals"],
    )
    assert len(snapshot) == 1
    first = snapshot[0]
    first_turn = {"session_id": "wake:default", "turn_id": "wake-1"}
    selected = drift.store.select(first["ref"], first_turn, NOW)
    runtime.observe_turn(
        TurnCommitted(
            session_key="wake:default",
            channel="wake",
            chat_id="chat",
            input_message="tick",
            persisted_user_message="tick",
            assistant_response="没有调用提交工具",
            tools_used=[],
            turn_id="wake-1",
            timestamp=NOW,
        )
    )
    _ = drift.store.transition(
        cast(str, selected["selection_token"]), "ready_for_delivery"
    )

    await runtime.tick_once()
    proposals = cast(
        tuple[dict[str, Any], ...],
        drift.store.snapshot(NOW)["proposals"],
    )
    second = next(item for item in proposals if item["ref"]["revision"] == "attempt-2")
    second_turn = {"session_id": "wake:default", "turn_id": "wake-2"}
    selected = drift.store.select(second["ref"], second_turn, NOW)
    arguments = {
        "proposal_id": "emotion-feedback:1-1",
        "revision": "attempt-2",
        "context": "用户愿意继续讨论明确引用的主题。",
        "candidates": [
            {
                "effect": "boost",
                "confidence": "medium",
                "topic": "明确引用的主题",
                "action": "提高同一主题后续候选的优先级",
                "evidence": [1],
            }
        ],
    }
    assert await runtime.commit_preference_context(object(), arguments) == {
        "committed": True,
        "duplicate": False,
    }
    assert await runtime.commit_preference_context(object(), arguments) == {
        "committed": False,
        "duplicate": True,
    }
    runtime.observe_turn(
        TurnCommitted(
            session_key="wake:default",
            channel="wake",
            chat_id="chat",
            input_message="tick",
            persisted_user_message="tick",
            assistant_response="已提交",
            tools_used=["emotion_commit_preference_context"],
            turn_id="wake-2",
            timestamp=NOW,
        )
    )
    runtime.observe_turn(
        TurnCommitted(
            session_key="wake:default",
            channel="wake",
            chat_id="chat",
            input_message="tick",
            persisted_user_message="tick",
            assistant_response="已提交",
            tools_used=["emotion_commit_preference_context"],
            turn_id="wake-2",
            timestamp=NOW,
        )
    )
    _ = drift.store.transition(
        cast(str, selected["selection_token"]), "ready_for_delivery"
    )

    conn = sqlite3.connect(emotion_root / "emotion.db")
    runs = conn.execute(
        "SELECT revision, status, result_json FROM emotion_drift_runs ORDER BY attempt"
    ).fetchall()
    assert [(row[0], row[1]) for row in runs] == [
        ("attempt-1", "completed_without_commit"),
        ("attempt-2", "completed"),
    ]
    assert runs[0][2] is None and runs[1][2] is not None
    assert conn.execute("SELECT count(*) FROM emotion_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM emotion_feedback_samples").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM emotion_effects").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM emotion_domain_effects").fetchone()[0] == 0
    conn.close()


class ManualHandle:
    def __init__(self, timer_id: str, deadline: datetime) -> None:
        self.id = timer_id
        self.deadline = deadline
        self._future: asyncio.Future[TimerReceipt] = asyncio.get_running_loop().create_future()

    async def result(self) -> TimerReceipt:
        return await asyncio.shield(self._future)

    async def cancel(self) -> TimerReceipt:
        if not self._future.done():
            self._future.set_result(self._receipt(TimerStatus.CANCELLED))
        return await asyncio.shield(self._future)

    async def cleanup(self) -> None:
        _ = await self.cancel()

    def fire(self) -> None:
        self._future.set_result(self._receipt(TimerStatus.FIRED))

    def _receipt(self, status: TimerStatus) -> TimerReceipt:
        return TimerReceipt(self.id, self.deadline, self.deadline, status)


class ManualTimer:
    def __init__(self) -> None:
        self.handles: list[ManualHandle] = []

    def schedule(self, deadline: datetime) -> ManualHandle:
        handle = ManualHandle(f"timer-{len(self.handles) + 1}", deadline)
        self.handles.append(handle)
        return handle

    @property
    def active(self) -> int:
        return sum(not handle._future.done() for handle in self.handles)


class RecordingContext:
    def __init__(self) -> None:
        self.incidents: list[tuple[str, str]] = []
        self.tasks: list[asyncio.Task[None]] = []

    async def spawn(self, coroutine: Any, *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task

    def report_incident(self, code: str, detail: str) -> None:
        self.incidents.append((code, detail))


class FailOnceDrift(EmptyDrift):
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary drift storage unavailable")
        return {"inserted": True}


class ContractBrokenDrift(EmptyDrift):
    def propose(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("drift contract mismatch")


async def _eventually(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not settle")


def _count_rows(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _history_record(cursor: int, **changes: object) -> SimpleNamespace:
    supplied_hash = changes.pop("payload_hash", None)
    values: dict[str, object] = {
        "cursor": cursor,
        "event_id": f"proactive_feedback:{cursor}",
        "session_key": "mobile:test",
        "user_message_id": f"u{cursor}",
        "assistant_message_id": f"a{cursor}",
        "proactive_message_id": f"p{cursor}",
        "feedback_type": "topic_follow",
        "confidence": "high",
        "pa_score": 0.8,
        "pua_score": 0.7,
        "lag_seconds": cursor,
        "candidate_count": 1,
        "matched_by": "pua",
        "reason": "fixture",
        "user_content_preview": "继续",
        "assistant_content_preview": "回答",
        "proactive_content_preview": "提醒",
    }
    values.update(changes)
    canonical = {
        field: values[field]
        for field in (
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
    values["payload_hash"] = supplied_hash or hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return SimpleNamespace(**values)


class RecordingHistory:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self.records = records
        self.requests: list[tuple[int, int]] = []

    def page(self, *, after_cursor: int, max_items: int) -> SimpleNamespace:
        self.requests.append((after_cursor, max_items))
        records = tuple(
            record for record in self.records if record.cursor > after_cursor
        )[:max_items]
        return SimpleNamespace(after_cursor=after_cursor, records=records)


class FailOnceHistory(RecordingHistory):
    def __init__(self, records: list[SimpleNamespace]) -> None:
        super().__init__(records)
        self.calls = 0

    def page(self, *, after_cursor: int, max_items: int) -> SimpleNamespace:
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary PF history I/O")
        return super().page(after_cursor=after_cursor, max_items=max_items)


async def _mount_formal_with_history(
    tmp_path: Path,
    *,
    order: tuple[str, str],
) -> tuple[CompositionRoot, ManualTimer, Path]:
    root = CompositionRoot("emotion-pf-" + "-".join(order))
    timer = ManualTimer()
    tools = PluginTools(root.instance_token)
    ui = PluginUiSlots()
    drift = EmptyDrift()
    history = RecordingHistory([_history_record(1)])
    _ = await root.context.provide(TIMERS, PluginTimers(timer))
    _ = await root.context.provide(TOOL_CATALOG, tools)
    _ = await root.context.provide(UI_SLOTS, ui)
    _ = await root.context.provide(module.DRIFT_PROPOSALS, drift)
    _ = await root.context.provide(module.DRIFT_WAKE, drift)
    emotion_root = tmp_path / "workspace" / "emotion"

    async def mount_emotion() -> None:
        _ = await root.mount(
            lambda ctx: module.apply(ctx, object()),
            name="emotion",
            inject=module.inject,
            runtime=PluginRuntime(
                plugin_id="emotion",
                plugin_dir=Path(__file__).parents[1],
                data_dir=tmp_path / "plugin-data" / "emotion",
                workspace=emotion_root.parent,
                config=None,
                workspace_roots=("emotion",),
                data_access="read_write",
            ),
        )

    async def mount_feedback() -> None:
        async def provider(ctx: Any) -> None:
            _ = await ctx.provide(module.PROACTIVE_FEEDBACK_HISTORY, history)

        _ = await root.mount(provider, name="proactive_feedback")

    for item in order:
        await (mount_emotion() if item == "emotion" else mount_feedback())
    _ = _freeze_plugin_tools(tools, root.instance_token, {"emotion": root.generation_id})
    return root, timer, emotion_root


def test_feedback_history_empty_page_creates_no_emotion_state(tmp_path: Path) -> None:
    history = RecordingHistory([])
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        cast(Any, history),
        now=lambda: NOW,
    )

    assert consumer.tick_once() is False
    assert history.requests == [(0, 50)]
    assert not (tmp_path / "emotion.db").exists()


def test_feedback_history_page_is_atomic_idempotent_and_detects_hash_drift(
    tmp_path: Path,
) -> None:
    history = RecordingHistory([_history_record(1), _history_record(2)])
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        cast(Any, history),
        now=lambda: NOW,
    )
    assert consumer.tick_once() is False
    assert consumer.tick_once() is False
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute("SELECT count(*) FROM emotion_events").fetchone() == (2,)
    assert connection.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone() == (2,)
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (2,)
    connection.close()
    connection = sqlite3.connect(tmp_path / "emotion.db")
    connection.execute(
        "UPDATE pf_history_cursor SET row_id=0 WHERE source='proactive_feedback'"
    )
    connection.commit()
    connection.close()
    history.records = [_history_record(1, payload_hash="f" * 64)]
    with pytest.raises(RuntimeError, match="payload hash 漂移"):
        consumer.tick_once()
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM emotion_events").fetchone() == (2,)
    connection.close()


@pytest.mark.parametrize(
    ("field", "mutated"),
    (
        ("session_key", "mobile:other"),
        ("user_message_id", "other-user"),
        ("assistant_message_id", "other-assistant"),
        ("proactive_message_id", None),
        ("feedback_type", "no_topic_follow"),
        ("confidence", "low"),
        ("pa_score", 0.1),
        ("pua_score", 0.2),
        ("lag_seconds", 99),
        ("candidate_count", 2),
        ("matched_by", "other-rule"),
        ("reason", "changed"),
        ("user_content_preview", None),
        ("assistant_content_preview", None),
        ("proactive_content_preview", None),
    ),
)
def test_feedback_history_recomputes_canonical_hash_before_apply(
    tmp_path: Path,
    field: str,
    mutated: object,
) -> None:
    original = _history_record(1)
    record = _history_record(
        1,
        **{field: mutated, "payload_hash": original.payload_hash},
    )
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        cast(Any, RecordingHistory([record])),
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="payload hash 漂移"):
        consumer.tick_once()
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM emotion_events").fetchone() == (0,)
    connection.close()


@pytest.mark.parametrize("score", (float("nan"), float("inf"), float("-inf")))
def test_feedback_history_rejects_nonfinite_scores(
    tmp_path: Path,
    score: float,
) -> None:
    record = _history_record(1, pa_score=score, payload_hash="0" * 64)
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        cast(Any, RecordingHistory([record])),
        now=lambda: NOW,
    )

    with pytest.raises(ValueError, match="pa_score 必须在"):
        consumer.tick_once()
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (0,)
    connection.close()


def test_legacy_pf_event_import_records_terminal_without_double_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "emotion.db"
    _create_formal_legacy_fixture(path)
    _insert_legacy_pf_event(path)
    upgraded = module.open_db(path)
    before_state = tuple(upgraded.execute(
        "SELECT valence, arousal, dominance FROM emotion_state WHERE id=1"
    ).fetchone())
    before_samples = upgraded.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone()[0]
    upgraded.close()

    history = RecordingHistory([_history_record(1)])
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        cast(Any, history),
        now=lambda: NOW,
    )
    assert consumer.tick_once() is False
    connection = sqlite3.connect(path)
    assert tuple(connection.execute(
        "SELECT valence, arousal, dominance FROM emotion_state WHERE id=1"
    ).fetchone()) == before_state
    assert connection.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone()[0] == before_samples
    receipt = connection.execute(
        "SELECT source_type, valence_delta, dominance_delta, payload_json "
        "FROM emotion_events WHERE source_event_id='pf_history_import:1'"
    ).fetchone()
    assert receipt[:3] == ("pf_history_import_terminal", 0.0, 0.0)
    receipt_payload = json.loads(receipt[3])
    assert receipt_payload["disposition"] == "legacy_event_already_applied"
    assert receipt_payload["legacy_event_id"] == "proactive_feedback:1"
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (1,)
    first_counts = (
        connection.execute("SELECT count(*) FROM emotion_events").fetchone()[0],
        connection.execute(
            "SELECT count(*) FROM emotion_feedback_samples"
        ).fetchone()[0],
    )
    connection.execute(
        "UPDATE pf_history_cursor SET row_id=0 WHERE source='proactive_feedback'"
    )
    connection.commit()
    connection.close()

    assert consumer.tick_once() is False
    connection = sqlite3.connect(path)
    assert (
        connection.execute("SELECT count(*) FROM emotion_events").fetchone()[0],
        connection.execute(
            "SELECT count(*) FROM emotion_feedback_samples"
        ).fetchone()[0],
    ) == first_counts
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (1,)
    connection.close()

    history.records.append(_history_record(2))
    assert consumer.tick_once() is False
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone()[0] == before_samples + 1
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (2,)
    assert connection.execute(
        "SELECT count(*) FROM emotion_events "
        "WHERE source_event_id='proactive_feedback:2'"
    ).fetchone() == (1,)
    connection.close()


def test_legacy_pf_event_identity_collision_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "emotion.db"
    _create_formal_legacy_fixture(path)
    _insert_legacy_pf_event(path, user_message_id="different-user")
    upgraded = module.open_db(path)
    before = (
        upgraded.execute("SELECT count(*) FROM emotion_events").fetchone()[0],
        upgraded.execute(
            "SELECT payload_json FROM emotion_events "
            "WHERE source_event_id='proactive_feedback:1'"
        ).fetchone()[0],
        tuple(upgraded.execute(
            "SELECT valence, arousal, dominance FROM emotion_state WHERE id=1"
        ).fetchone()),
        upgraded.execute(
            "SELECT count(*) FROM emotion_feedback_samples"
        ).fetchone()[0],
    )
    upgraded.close()
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()),
        tmp_path,
        PluginTimers.candidate_validation(),
        cast(Any, RecordingHistory([_history_record(1)])),
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="legacy identity 冲突"):
        consumer.tick_once()
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM emotion_events "
        "WHERE source_event_id='pf_history_import:1'"
    ).fetchone() == (0,)
    after = (
        connection.execute("SELECT count(*) FROM emotion_events").fetchone()[0],
        connection.execute(
            "SELECT payload_json FROM emotion_events "
            "WHERE source_event_id='proactive_feedback:1'"
        ).fetchone()[0],
        tuple(connection.execute(
            "SELECT valence, arousal, dominance FROM emotion_state WHERE id=1"
        ).fetchone()),
        connection.execute(
            "SELECT count(*) FROM emotion_feedback_samples"
        ).fetchone()[0],
    )
    connection.close()
    assert after == before


def test_pf_explicit_quote_receipt_does_not_double_apply_direct_signal(
    tmp_path: Path,
) -> None:
    module._on_turn_committed(_feedback_turn(), root=tmp_path)
    connection = sqlite3.connect(tmp_path / "emotion.db")
    before = connection.execute(
        "SELECT valence, dominance FROM emotion_state WHERE id=1"
    ).fetchone()
    assert connection.execute(
        "SELECT source_plugin FROM emotion_events "
        "WHERE source_event_id='emotion_explicit_quote:turn-feedback-1'"
    ).fetchone() == ("emotion",)
    assert connection.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone() == (1,)
    connection.close()
    record = _history_record(
        1,
        feedback_type="explicit_quote",
        confidence="gold",
        user_message_id="u1",
        matched_by="explicit_quote",
        reason="explicit_quote",
    )
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()), tmp_path, PluginTimers.candidate_validation(),
        cast(Any, RecordingHistory([record])), now=lambda: NOW,
    )

    assert consumer.tick_once() is False
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute("SELECT count(*) FROM emotion_events").fetchone() == (2,)
    assert connection.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone() == (1,)
    terminal = connection.execute(
        "SELECT source_type, reason, valence_delta, dominance_delta "
        "FROM emotion_events WHERE source_event_id='proactive_feedback:1'"
    ).fetchone()
    assert terminal == (
        "explicit_quote_already_applied",
        "direct_quote_already_applied",
        0.0,
        0.0,
    )
    assert connection.execute(
        "SELECT valence, dominance FROM emotion_state WHERE id=1"
    ).fetchone() == before
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (1,)
    connection.close()

def test_feedback_history_page_failure_rolls_back_all_derived_facts(tmp_path: Path) -> None:
    connection = module.open_db(tmp_path / "emotion.db")
    connection.execute(
        """
        CREATE TRIGGER reject_second_feedback
        BEFORE INSERT ON emotion_events
        WHEN NEW.source_event_id = 'proactive_feedback:2'
        BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END
        """
    )
    connection.commit()
    connection.close()
    history = RecordingHistory([_history_record(1), _history_record(2)])
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, object()), tmp_path, PluginTimers.candidate_validation(),
        cast(Any, history), now=lambda: NOW,
    )
    with pytest.raises(sqlite3.IntegrityError, match="fixture rejection"):
        consumer.tick_once()
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute("SELECT count(*) FROM emotion_events").fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM emotion_feedback_samples"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (0,)
    connection.execute("DROP TRIGGER reject_second_feedback")
    connection.commit()
    connection.close()
    restarted = module.FeedbackHistoryConsumer(
        cast(Any, object()), tmp_path, PluginTimers.candidate_validation(),
        cast(Any, history), now=lambda: NOW,
    )
    assert restarted.tick_once() is False
    connection = sqlite3.connect(tmp_path / "emotion.db")
    assert connection.execute("SELECT count(*) FROM emotion_events").fetchone() == (2,)
    assert connection.execute(
        "SELECT row_id FROM pf_history_cursor WHERE source='proactive_feedback'"
    ).fetchone() == (2,)
    connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    (("emotion", "feedback"), ("feedback", "emotion")),
)
async def test_feedback_history_composes_in_both_mount_orders_and_uses_timer(
    tmp_path: Path,
    order: tuple[str, str],
) -> None:
    root, timer, emotion_root = await _mount_formal_with_history(
        tmp_path / "-".join(order), order=order
    )
    try:
        await root.context.serial(RUNTIME_STARTED, RuntimeStarted())
        await _eventually(lambda: len(timer.handles) >= 1)
        feedback_timer = min(timer.handles, key=lambda handle: handle.deadline)
        assert not (emotion_root / "emotion.db").exists() or _count_rows(
            emotion_root / "emotion.db", "emotion_feedback_samples"
        ) == 0
        feedback_timer.fire()
        await _eventually(
            lambda: (emotion_root / "emotion.db").exists()
            and _count_rows(
                emotion_root / "emotion.db", "emotion_feedback_samples"
            ) == 1
        )
        assert sum(not handle._future.done() for handle in timer.handles) == 2
        await root.context.serial(RUNTIME_STOPPING, RuntimeStopping())
        assert timer.active == 0
    finally:
        await root.dispose()


@pytest.mark.asyncio
async def test_feedback_history_candidate_handshake_has_zero_timer_and_db(
    tmp_path: Path,
) -> None:
    root, emotion_root, _ = await _mount_candidate(tmp_path)
    history = RecordingHistory([_history_record(1)])
    try:
        async def provider(ctx: Any) -> None:
            _ = await ctx.provide(module.PROACTIVE_FEEDBACK_HISTORY, history)

        _ = await root.mount(provider, name="proactive_feedback")
        assert any(
            fiber.name == "proactive-feedback-history" and fiber.state.value == "active"
            for fiber in root.root_fiber.children
            for fiber in fiber.children
        )
        assert history.requests == []
        assert not (emotion_root / "emotion.db").exists()
    finally:
        await root.dispose()


@pytest.mark.asyncio
async def test_feedback_history_transient_failure_rearms_and_recovers(
    tmp_path: Path,
) -> None:
    timer = ManualTimer()
    context = RecordingContext()
    history = FailOnceHistory([_history_record(1)])
    consumer = module.FeedbackHistoryConsumer(
        cast(Any, context), tmp_path, PluginTimers(timer), cast(Any, history),
        now=lambda: NOW,
    )
    await consumer.start()
    await _eventually(lambda: len(timer.handles) == 1)
    timer.handles[0].fire()
    await _eventually(lambda: len(timer.handles) == 2)
    assert context.incidents[0][0] == "emotion_feedback_history_transient"
    assert not (tmp_path / "emotion.db").exists()
    timer.handles[1].fire()
    await _eventually(
        lambda: (tmp_path / "emotion.db").exists()
        and _count_rows(tmp_path / "emotion.db", "emotion_events") == 1
    )
    assert len(timer.handles) == 3
    await consumer.close()
    assert timer.active == 0


@pytest.mark.asyncio
async def test_transient_tick_is_observable_and_rearms_without_state_pollution(
    tmp_path: Path,
) -> None:
    module._on_turn_committed(_feedback_turn(), root=tmp_path)
    timer = ManualTimer()
    context = RecordingContext()
    drift = FailOnceDrift()
    runtime = module.EmotionRuntime(
        cast(Any, context),
        tmp_path,
        PluginTimers(timer),
        drift,
        drift,
        now=lambda: NOW,
    )
    await runtime.start()
    await _eventually(lambda: len(timer.handles) == 1)
    assert context.incidents[0][0] == "emotion_tick_transient"
    assert timer.active == 1
    timer.handles[0].fire()
    await _eventually(lambda: len(timer.handles) == 2)
    assert drift.calls == 2
    assert timer.active == 1
    await runtime.close()
    assert timer.active == 0

    replacement = module.EmotionRuntime(
        cast(Any, context),
        tmp_path,
        PluginTimers(timer),
        drift,
        drift,
        now=lambda: NOW,
    )
    await replacement.start()
    await _eventually(lambda: len(timer.handles) == 3)
    assert timer.active == 1
    await replacement.close()
    assert timer.active == 0


@pytest.mark.asyncio
async def test_contract_failure_stops_without_rearming_and_stays_observable(
    tmp_path: Path,
) -> None:
    module._on_turn_committed(_feedback_turn(), root=tmp_path)
    timer = ManualTimer()
    context = RecordingContext()
    drift = ContractBrokenDrift()
    runtime = module.EmotionRuntime(
        cast(Any, context),
        tmp_path,
        PluginTimers(timer),
        drift,
        drift,
        now=lambda: NOW,
    )
    await runtime.start()
    await _eventually(lambda: bool(context.tasks) and context.tasks[0].done())
    with pytest.raises(RuntimeError, match="drift contract mismatch"):
        _ = context.tasks[0].result()
    assert timer.handles == []
    assert context.incidents == []
    await runtime.close()
