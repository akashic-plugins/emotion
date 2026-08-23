from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from bus.events_lifecycle import TurnCommitted


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_module(Path(__file__).parents[1] / "plugin.py", "test_emotion_sample_plugin")
SAMPLE = _load_module(
    Path(__file__).parents[1]
    / "drift/skills/feedback-preference-context/scripts/sample_feedback_context.py",
    "test_emotion_feedback_sample",
)


def _turn() -> TurnCommitted:
    return TurnCommitted(
        session_key="sample:test",
        channel="test",
        chat_id="chat",
        input_message="被回复消息：主动提醒关于迁移的主题\n\n【你当前新消息】继续这个主题",
        persisted_user_message="被回复消息：主动提醒关于迁移的主题\n\n【你当前新消息】继续这个主题",
        assistant_response="好的，我继续整理。",
        tools_used=[],
        turn_id="turn-sample-1",
        persisted_user_message_id="user-sample-1",
        assistant_message_id="assistant-sample-1",
        timestamp=datetime.now(timezone.utc),
        extra={
            "proactive_feedback": {
                "event_id": "feedback-sample-1",
                "feedback_type": "topic_follow",
                "confidence": "high",
                "proactive_message_id": "proactive-sample-1",
                "proactive_content_preview": "主动提醒关于迁移的主题",
                "pua_score": 0.91,
                "candidate_count": 2,
                "matched_by": "typed_turn",
                "reason": "topic_follow_high",
            }
        },
    )


def test_typed_turn_feedback_is_readable_without_legacy_databases(tmp_path: Path) -> None:
    emotion_root = tmp_path / "emotion"
    PLUGIN._on_turn_committed(_turn(), root=emotion_root)

    old_feedback = tmp_path / "proactive_feedback"
    old_feedback.mkdir()
    (old_feedback / "proactive_feedback.db").write_text("not sqlite", encoding="utf-8")
    (tmp_path / "sessions.db").write_text("not sqlite", encoding="utf-8")

    result = SAMPLE.sample(tmp_path / "drift", 50, 10, 0)

    assert result["found"] is True
    assert result["count"] == 1
    event = result["events"][0]
    assert event["feedback_type"] == "explicit_quote"
    assert event["message_ids"] == {
            "proactive": "",
        "user": "user-sample-1",
    }
    assert event["texts"] == {
        "proactive": "主动提醒关于迁移的主题",
        "user": "被回复消息：主动提醒关于迁移的主题 【你当前新消息】继续这个主题",
    }


def test_feedback_sample_missing_does_not_fallback_to_legacy_owner(tmp_path: Path) -> None:
    old_feedback = tmp_path / "proactive_feedback"
    old_feedback.mkdir()
    (old_feedback / "proactive_feedback.db").write_text("not sqlite", encoding="utf-8")
    (tmp_path / "sessions.db").write_text("not sqlite", encoding="utf-8")

    result = SAMPLE.sample(tmp_path / "drift", 50, 10, 0)

    assert result["found"] is False
    assert result["reason"] == "emotion_db_missing"
    assert not (tmp_path / "emotion" / "emotion.db").exists()


def test_feedback_sample_empty_is_closed_without_creating_legacy_reads(tmp_path: Path) -> None:
    db = PLUGIN.open_db(tmp_path / "emotion" / "emotion.db")
    db.close()

    result = SAMPLE.sample(tmp_path / "drift", 50, 10, 0)

    assert result["found"] is False
    assert result["reason"] == "emotion_feedback_samples_empty"
    assert not (tmp_path / "sessions.db").exists()
