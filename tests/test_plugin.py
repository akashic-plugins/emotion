from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.plugins.context import PluginContext, PluginKVStore
from agent.plugins.scope import PluginScope, ScopedEventBus
from bus.event_bus import EventBus
from bus.events_proactive import ProactiveFeedbackRecorded


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
EmotionPlugin = module.EmotionPlugin


def _plugin_context(tmp_path: Path) -> PluginContext:
    scope = PluginScope("emotion")
    return PluginContext(
        event_bus=ScopedEventBus(EventBus(), scope),
        tool_registry=None,
        plugin_id="emotion",
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        kv_store=PluginKVStore(tmp_path / ".kv.json"),
        workspace=tmp_path,
        scope=scope,
    )


@pytest.mark.asyncio
async def test_emotion_plugin_activates_and_reads_state(tmp_path: Path) -> None:
    plugin = EmotionPlugin()
    plugin.context = _plugin_context(tmp_path)
    plugin.activate()
    try:
        plugin._on_feedback_recorded(
            ProactiveFeedbackRecorded(
                event_id=1,
                session_key="telegram:1",
                user_message_id="u1",
                assistant_message_id="a1",
                proactive_message_id="p1",
                feedback_type="topic_follow",
                confidence="high",
                pua_score=0.7,
                lag_seconds=1,
                matched_by="recent_pua",
            )
        )
        state = await plugin.get_emotion_state(None)
    finally:
        await plugin.terminate()
    assert state["available"] is True


@pytest.mark.asyncio
async def test_mobile_projection_returns_state_and_real_influences(tmp_path: Path) -> None:
    plugin = EmotionPlugin()
    plugin.context = _plugin_context(tmp_path)
    plugin.activate()
    try:
        base = datetime(2026, 7, 17, tzinfo=timezone.utc)
        db = module.open_db(tmp_path / "emotion" / "emotion.db")
        try:
            for index in range(10):
                module.build_effect(
                    db,
                    tick_id=f"noise:{index}",
                    session_key="proactive:default",
                    now_utc=base + timedelta(minutes=index),
                    last_user_at=base,
                    base_threshold=0.6,
                )
        finally:
            db.close()
        for event_id in range(1, 5):
            plugin._on_feedback_recorded(
                ProactiveFeedbackRecorded(
                    event_id=event_id,
                    session_key="mobile:test",
                    user_message_id=f"u-mobile-{event_id}",
                    assistant_message_id=f"a-mobile-{event_id}",
                    proactive_message_id=f"p-mobile-{event_id}",
                    feedback_type="explicit_quote",
                    confidence="gold",
                    pua_score=None,
                    lag_seconds=4,
                    matched_by="quote",
                )
            )
        plugin._on_feedback_recorded(
            ProactiveFeedbackRecorded(
                event_id=5,
                session_key="mobile:test",
                user_message_id="u-neutral",
                assistant_message_id="a-neutral",
                proactive_message_id="p-neutral",
                feedback_type="unscored",
                confidence="low",
                pua_score=None,
                lag_seconds=4,
                matched_by="recent_pua",
            )
        )
        bootstrap = plugin.mobile_ui_query(
            "emotion.bootstrap",
            {"limit": 10},
            session_id=None,
            turn_id=None,
        )
        overview = bootstrap["overview"]
        history = {"items": bootstrap["items"]}
    finally:
        await plugin.terminate()

    assert overview["effect_count"] == 10
    assert overview["event_count"] == 5
    assert overview["influence_count"] == 4
    assert overview["last_effect"]["expected_effect"] == "tone_only"
    assert overview["current_behavior"]["expected_effect"] == "lower_send_bar"
    assert len(history["items"]) == 4
    assert {item["source_type"] for item in history["items"]} == {"explicit_quote"}


@pytest.mark.asyncio
async def test_mobile_projection_rejects_invalid_limit(tmp_path: Path) -> None:
    plugin = EmotionPlugin()
    plugin.context = _plugin_context(tmp_path)

    with pytest.raises(ValueError, match="limit 必须"):
        plugin.mobile_ui_query(
            "emotion.bootstrap",
            {"limit": True},
            session_id=None,
            turn_id=None,
        )


def test_domain_effect_receipt_is_atomic_idempotent_and_durable(tmp_path: Path) -> None:
    db_path = tmp_path / "emotion" / "emotion.db"
    conn = module.open_db(db_path)
    try:
        digest = hashlib.sha256(b"merged-documents").hexdigest()
        committed = module.commit_domain_effect(
            conn,
            semantic_job_id="emotion:merge_proactive_pending",
            event_id="drift-1",
            invocation_id="invocation-1",
            effect_id="emotion.state",
            idempotency_key="emotion:merge_proactive_pending:event:drift-1",
            attempt=1,
            result_digest=digest,
        )
        repeated = module.commit_domain_effect(
            conn,
            semantic_job_id="emotion:merge_proactive_pending",
            event_id="drift-1",
            invocation_id="invocation-1",
            effect_id="emotion.state",
            idempotency_key="emotion:merge_proactive_pending:event:drift-1",
            attempt=1,
            result_digest=digest,
        )
    finally:
        conn.close()

    restarted = module.open_db(db_path)
    try:
        found = module.lookup_domain_effect(
            restarted,
            invocation_id="invocation-1",
            effect_id="emotion.state",
            idempotency_key="emotion:merge_proactive_pending:event:drift-1",
        )
        rows = restarted.execute(
            "SELECT COUNT(*) FROM emotion_domain_effects"
        ).fetchone()
        with pytest.raises(RuntimeError, match="identity 漂移"):
            module.commit_domain_effect(
                restarted,
                semantic_job_id="emotion:merge_proactive_pending",
                event_id="drift-1",
                invocation_id="invocation-2",
                effect_id="emotion.state",
                idempotency_key="emotion:merge_proactive_pending:event:drift-1",
                attempt=1,
                result_digest=digest,
            )
    finally:
        restarted.close()

    assert committed == repeated == found
    assert rows is not None and int(rows[0]) == 1
