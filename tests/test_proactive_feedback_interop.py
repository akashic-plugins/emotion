from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from agent.plugin_composition import PluginTimers
from bus.events_lifecycle import TurnCommitted



def _load_emotion_module() -> ModuleType:
    path = Path(__file__).parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "emotion_pf_interop_plugin",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EMOTION = _load_emotion_module()


def _load_feedback_modules() -> tuple[ModuleType, ModuleType]:
    root_value = os.environ.get("AKASHIC_PROACTIVE_FEEDBACK_ROOT", "").strip()
    if not root_value:
        raise RuntimeError(
            "AKASHIC_PROACTIVE_FEEDBACK_ROOT 必须指向 exact Feedback checkout"
        )
    root = Path(root_value)
    package = ModuleType("emotion_pf_interop")
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules[package.__name__] = package
    return (
        importlib.import_module("emotion_pf_interop.db"),
        importlib.import_module("emotion_pf_interop.history"),
    )


def test_pf_accepted_fact_reaches_emotion_only_after_history_pull(
    tmp_path: Path,
) -> None:
    pf_db, pf_history = _load_feedback_modules()
    assert (
        pf_history.PROACTIVE_FEEDBACK_HISTORY
        == EMOTION.PROACTIVE_FEEDBACK_HISTORY
    )
    feedback_path = tmp_path / "feedback" / "proactive_feedback.db"
    connection = pf_db.open_db(feedback_path)
    try:
        row_id = pf_db.insert_feedback(
            connection,
            pf_db.FeedbackEvent(
                session_key="mobile:interop",
                user_message_id="u1",
                assistant_message_id="a1",
                proactive_message_id="p1",
                feedback_type="topic_follow",
                confidence="high",
                pa_score=0.9,
                pua_score=0.8,
                lag_seconds=12,
                candidate_count=1,
                matched_by="pua",
                reason="fixture",
                user_content_preview="好呀，我晚上去",
                assistant_content_preview="好，晚上更凉快",
                proactive_content_preview="记得今天散步",
            ),
        )
    finally:
        connection.close()
    assert row_id == 1

    emotion_root = tmp_path / "emotion"
    EMOTION._on_turn_committed(
        TurnCommitted(
            session_key="mobile:interop",
            channel="test",
            chat_id="chat",
            input_message="好呀，我晚上去",
            persisted_user_message="好呀，我晚上去",
            assistant_response="好，晚上更凉快",
            tools_used=[],
            turn_id="turn-1",
            persisted_user_message_id="u1",
            assistant_message_id="a1",
            extra={"proactive_feedback": {"feedback_type": "topic_follow"}},
        ),
        root=emotion_root,
    )
    assert not (emotion_root / "emotion.db").exists()

    consumer = EMOTION.FeedbackHistoryConsumer(
        cast(Any, object()),
        emotion_root,
        PluginTimers.candidate_validation(),
        pf_history.SqliteFeedbackHistory(feedback_path),
    )
    assert consumer.tick_once() is False
    assert consumer.tick_once() is False
    with sqlite3.connect(emotion_root / "emotion.db") as emotion:
        assert emotion.execute(
            "SELECT count(*) FROM emotion_feedback_samples"
        ).fetchone() == (1,)
        assert emotion.execute(
            "SELECT count(*) FROM emotion_events"
        ).fetchone() == (1,)
        assert emotion.execute(
            "SELECT row_id FROM pf_history_cursor "
            "WHERE source='proactive_feedback'"
        ).fetchone() == (1,)
