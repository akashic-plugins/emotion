from __future__ import annotations

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
async def test_emotion_plugin_initializes_and_reads_state(tmp_path: Path) -> None:
    plugin = EmotionPlugin()
    plugin.context = _plugin_context(tmp_path)
    await plugin.initialize()
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
    await plugin.initialize()
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
        overview = await plugin.mobile_ui_call(
            "emotion.overview",
            {},
            session_id=None,
            turn_id=None,
        )
        history = await plugin.mobile_ui_call(
            "emotion.influences",
            {"limit": 10},
            session_id=None,
            turn_id=None,
        )
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
        await plugin.mobile_ui_call(
            "emotion.influences",
            {"limit": True},
            session_id=None,
            turn_id=None,
        )
