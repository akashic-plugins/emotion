from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI

from .db import EmotionState, describe_behavior


class EmotionDashboardReader:
    def __init__(self, workspace: Path) -> None:
        self.db_path = workspace / "emotion" / "emotion.db"
        self.sessions_db_path = workspace / "sessions.db"
        self._lock = threading.RLock()

    def get_overview(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return _empty_overview()
        with self._lock:
            with _connect(self.db_path) as db:
                return _overview_from_db(db)

    def list_influences(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """返回真正改变主动状态的近期反馈。"""

        # 1. 只读取产生状态增量的反馈，跳过每次主动 tick 的重复 effect
        if not self.db_path.exists():
            return []
        safe_limit = max(1, min(limit, 50))
        with self._lock:
            with _connect(self.db_path) as db:
                rows = _influence_rows(db, safe_limit)

        # 2. 用事件已持有的消息 ID 补齐可读预览
        decoded = [_decode_influence(row) for row in rows]
        previews = self._load_user_previews(decoded)
        for item in decoded:
            item["user_preview"] = _preview(previews.get(str(item["user_message_id"])))
        return decoded

    def get_mobile_bootstrap(self, *, limit: int = 30) -> dict[str, Any]:
        """从一个已提交快照返回移动首屏状态和影响列表。"""

        # 1. 同一个显式读事务固定 emotion DB 快照
        if not self.db_path.exists():
            return {"overview": _empty_overview(), "items": []}
        safe_limit = max(1, min(limit, 50))
        with self._lock:
            with _connect(self.db_path) as db:
                _ = db.execute("BEGIN")
                overview = _overview_from_db(db)
                decoded = [_decode_influence(row) for row in _influence_rows(db, safe_limit)]

        # 2. 会话预览是独立数据源，只补充文案，不参与 emotion 状态一致性
        previews = self._load_user_previews(decoded)
        for item in decoded:
            item["user_preview"] = _preview(previews.get(str(item["user_message_id"])))
        return {"overview": overview, "items": decoded}

    def list_effects(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.db_path.exists():
            return [], 0
        safe_page = max(1, page)
        safe_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_size
        with self._lock:
            with _connect(self.db_path) as db:
                total = _scalar_int(db, "SELECT count(*) FROM emotion_effects")
                rows = db.execute(
                    """
                    SELECT *
                    FROM emotion_effects
                    ORDER BY created_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (safe_size, offset),
                ).fetchall()
        return [_decode_effect(row) for row in rows], total

    def get_effect(self, effect_id: int) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with self._lock:
            with _connect(self.db_path) as db:
                row = db.execute(
                    "SELECT * FROM emotion_effects WHERE id = ?",
                    (effect_id,),
                ).fetchone()
        return _decode_effect(row) if row is not None else None

    def _load_user_previews(self, items: list[dict[str, Any]]) -> dict[str, str]:
        if not items or not self.sessions_db_path.exists():
            return {}
        ids = list(dict.fromkeys(str(item["user_message_id"]) for item in items))
        placeholders = ",".join("?" for _ in ids)
        with _connect(self.sessions_db_path) as db:
            rows = db.execute(
                f"SELECT id, content FROM messages WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        return {str(row["id"]): str(row["content"] or "") for row in rows}


def register(app: FastAPI, plugin_dir: Path, workspace: Path) -> None:
    _ = plugin_dir
    reader = EmotionDashboardReader(workspace)

    @app.get("/api/dashboard/emotion/overview")
    def get_emotion_overview() -> dict[str, Any]:
        return reader.get_overview()

    @app.get("/api/dashboard/emotion/effects")
    def list_emotion_effects(
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        items, total = reader.list_effects(page=page, page_size=page_size)
        return {
            "items": items,
            "total": total,
            "page": max(1, page),
            "page_size": max(1, min(page_size, 100)),
        }

    @app.get("/api/dashboard/emotion/effects/{effect_id}")
    def get_emotion_effect(effect_id: int) -> dict[str, Any]:
        return reader.get_effect(effect_id) or {}


def _empty_overview() -> dict[str, Any]:
    return {
        "state": None,
        "current_behavior": None,
        "event_count": 0,
        "influence_count": 0,
        "effect_count": 0,
        "last_effect_at": None,
        "last_effect": None,
    }


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _scalar_int(db: sqlite3.Connection, sql: str) -> int:
    row = db.execute(sql).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _overview_from_db(db: sqlite3.Connection) -> dict[str, Any]:
    state = db.execute(
        "SELECT valence, arousal, dominance, updated_at FROM emotion_state WHERE id = 1"
    ).fetchone()
    last_effect = db.execute(
        """
        SELECT created_at, tone_label, expected_effect, threshold_delta
        FROM emotion_effects
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    current_behavior = None
    if state is not None:
        behavior = describe_behavior(
            EmotionState(
                valence=float(state["valence"]),
                arousal=float(state["arousal"]),
                dominance=float(state["dominance"]),
                updated_at=str(state["updated_at"]),
            )
        )
        current_behavior = {
            "tone_label": behavior.tone_label,
            "expected_effect": behavior.expected_effect,
            "threshold_delta": behavior.threshold_delta,
        }
    return {
        "state": dict(state) if state is not None else None,
        "current_behavior": current_behavior,
        "event_count": _scalar_int(db, "SELECT count(*) FROM emotion_events"),
        "influence_count": _scalar_int(
            db,
            """
            SELECT count(*) FROM emotion_events
            WHERE abs(valence_delta) > 0.000001
               OR abs(dominance_delta) > 0.000001
            """,
        ),
        "effect_count": _scalar_int(db, "SELECT count(*) FROM emotion_effects"),
        "last_effect_at": last_effect["created_at"] if last_effect else None,
        "last_effect": dict(last_effect) if last_effect is not None else None,
    }


def _influence_rows(db: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT
            id,
            created_at,
            source_type,
            valence_delta,
            dominance_delta,
            valence_after,
            dominance_after,
            reason,
            payload_json
        FROM emotion_events
        WHERE abs(valence_delta) > 0.000001
           OR abs(dominance_delta) > 0.000001
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _decode_effect(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    try:
        payload["metadata"] = json.loads(str(payload.pop("metadata_json") or "{}"))
    except Exception:
        payload["metadata"] = {}
    return payload


def _decode_influence(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    metadata = json.loads(str(payload.pop("payload_json")))
    payload["user_message_id"] = str(metadata["user_message_id"])
    return payload


def _preview(value: str | None, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
