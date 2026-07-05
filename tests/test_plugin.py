from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from agent.plugins.context import PluginContext, PluginKVStore
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


@pytest.mark.asyncio
async def test_emotion_plugin_initializes_and_reads_state(tmp_path: Path) -> None:
    plugin = EmotionPlugin()
    plugin.context = PluginContext(
        event_bus=EventBus(),
        tool_registry=None,
        plugin_id="emotion",
        plugin_dir=tmp_path,
        data_dir=tmp_path,
        kv_store=PluginKVStore(tmp_path / ".kv.json"),
        workspace=tmp_path,
    )
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
