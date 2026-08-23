from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def compute_energy(
    last_user_at: datetime | None,
    now: datetime | None = None,
    *,
    alpha: float = 0.50,
    beta: float = 0.35,
    gamma: float = 0.15,
    tau1_min: float = 30.0,
    tau2_min: float = 240.0,
    tau3_min: float = 2880.0,
) -> float:
    """Return the current interaction energy without depending on Core internals."""

    if last_user_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    minutes = max(0.0, (now - last_user_at).total_seconds() / 60.0)
    return (
        alpha * math.exp(-minutes / tau1_min)
        + beta * math.exp(-minutes / tau2_min)
        + gamma * math.exp(-minutes / tau3_min)
    )


@dataclass(frozen=True)
class EmotionState:
    valence: float
    arousal: float
    dominance: float
    updated_at: str


@dataclass(frozen=True)
class FeedbackDelta:
    valence: float
    dominance: float
    reason: str


@dataclass(frozen=True)
class EmotionBehavior:
    tone_label: str
    tone_instruction: str
    threshold_delta: float
    expected_effect: str


_SCHEMA_VERSION = 1
_TABLE_SQL = {
    "emotion_state": """
        CREATE TABLE emotion_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            valence REAL NOT NULL,
            arousal REAL NOT NULL,
            dominance REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
    "emotion_events": """
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
        )
    """,
    "emotion_feedback_samples": """
        CREATE TABLE emotion_feedback_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_event_id TEXT NOT NULL UNIQUE,
            session_key TEXT NOT NULL,
            user_message_id TEXT,
            assistant_message_id TEXT,
            proactive_message_id TEXT,
            feedback_type TEXT NOT NULL,
            confidence TEXT NOT NULL,
            pa_score REAL,
            pua_score REAL,
            lag_seconds INTEGER,
            candidate_count INTEGER,
            matched_by TEXT,
            reason TEXT,
            user_content_preview TEXT,
            assistant_content_preview TEXT,
            proactive_content_preview TEXT
        )
    """,
    "emotion_effects": """
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
        )
    """,
    "emotion_domain_effects": """
        CREATE TABLE emotion_domain_effects (
            semantic_job_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            effect_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            result_digest TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (invocation_id, effect_id, idempotency_key),
            UNIQUE (semantic_job_id, event_id, effect_id)
        )
    """,
    "emotion_context_current": """
        CREATE TABLE emotion_context_current (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_user_at TEXT,
            presence TEXT NOT NULL,
            prompt_section TEXT NOT NULL,
            source_state_updated_at TEXT NOT NULL,
            refreshed_at TEXT NOT NULL
        )
    """,
    "emotion_preference_state": """
        CREATE TABLE emotion_preference_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            context_text TEXT NOT NULL,
            processed_feedback_sample_id INTEGER NOT NULL CHECK (
                processed_feedback_sample_id >= 0
            ),
            updated_at TEXT NOT NULL
        )
    """,
    "emotion_drift_runs": """
        CREATE TABLE emotion_drift_runs (
            proposal_id TEXT NOT NULL,
            revision TEXT NOT NULL,
            sample_first_id INTEGER NOT NULL,
            sample_last_id INTEGER NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            context_before TEXT NOT NULL,
            result_json TEXT,
            selected_session_id TEXT,
            selected_turn_id TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (proposal_id, revision)
        )
    """,
}
_LEGACY_TABLE_SETS = frozenset(
    {
        frozenset(),
        frozenset({"emotion_state", "emotion_events", "emotion_effects"}),
        frozenset(
            {
                "emotion_state",
                "emotion_events",
                "emotion_effects",
                "emotion_domain_effects",
            }
        ),
        frozenset(
            {
                "emotion_state",
                "emotion_events",
                "emotion_feedback_samples",
                "emotion_effects",
                "emotion_domain_effects",
            }
        ),
        frozenset(_TABLE_SQL),
    }
)


def open_db(path: Path) -> sqlite3.Connection:
    """Validate, atomically upgrade, and open the exact Emotion database."""

    # 1. Existing bytes are inspected read-only before any directory, pragma, or DDL write.
    if path.exists():
        with _connect_read_only(path) as existing:
            _validate_schema(existing)

    # 2. Revalidate under the write lock, then create all missing tables in one transaction.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        _validate_schema(conn)
        existing_tables = _owned_tables(conn)
        for table_name, table_sql in _TABLE_SQL.items():
            if table_name not in existing_tables:
                conn.execute(table_sql)
        now = datetime.now(timezone.utc).isoformat()
        _ = conn.execute(
            """
            INSERT OR IGNORE INTO emotion_state(
                id, valence, arousal, dominance, updated_at
            ) VALUES(1, 0.0, 0.0, 0.0, ?)
            """,
            (now,),
        )
        _ = conn.execute(
            """
            INSERT OR IGNORE INTO emotion_preference_state(
                id, context_text, processed_feedback_sample_id, updated_at
            ) VALUES(1, '', 0, ?)
            """,
            (now,),
        )
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    _ = conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _validate_schema(conn: sqlite3.Connection) -> None:
    """Accept only exact historical or current Emotion table topologies."""

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, _SCHEMA_VERSION}:
        raise RuntimeError(f"不支持的 Emotion schema version: {version}")
    tables = _owned_tables(conn)
    unknown = set(tables).difference(_TABLE_SQL)
    if unknown:
        raise RuntimeError("Emotion table set 不匹配")
    for table_name, actual_sql in tables.items():
        if _normalize_sql(actual_sql) != _normalize_sql(_TABLE_SQL[table_name]):
            raise RuntimeError(f"Emotion table schema 不匹配: {table_name}")
    if version == 0 and frozenset(tables) not in _LEGACY_TABLE_SETS:
        raise RuntimeError("Emotion legacy table set 不匹配")
    if version == _SCHEMA_VERSION and frozenset(tables) != frozenset(_TABLE_SQL):
        raise RuntimeError("Emotion current table set 不匹配")
    check = conn.execute("PRAGMA quick_check").fetchone()
    if check is None or check[0] != "ok":
        raise RuntimeError("Emotion SQLite quick_check failed")


def _owned_tables(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {str(row["name"]): str(row["sql"]) for row in rows}


def _normalize_sql(sql: str) -> str:
    return "".join(sql.lower().split())


def record_user_activity(conn: sqlite3.Connection, at: datetime) -> None:
    """Update only the current user-presence input without creating history."""

    instant = _aware_utc(at)
    _ = conn.execute(
        """
        INSERT INTO emotion_context_current(
            id, last_user_at, presence, prompt_section,
            source_state_updated_at, refreshed_at
        ) VALUES(1, ?, 'active', '', ?, ?)
        ON CONFLICT(id) DO UPDATE SET last_user_at = excluded.last_user_at
        """,
        (instant, instant, instant),
    )
    conn.commit()


def refresh_current_context(
    conn: sqlite3.Connection,
    *,
    now: datetime,
) -> dict[str, object]:
    """Refresh the overwriteable VAD/presence card without appending history."""

    # 1. Decay the current state and derive presence from the latest committed user turn.
    instant = _aware_datetime(now)
    state = _decay(get_state(conn), instant.isoformat())
    row = conn.execute(
        "SELECT last_user_at FROM emotion_context_current WHERE id = 1"
    ).fetchone()
    last_user_at = _optional_datetime(None if row is None else row["last_user_at"])
    energy = compute_energy(last_user_at, instant)
    state = EmotionState(
        valence=state.valence,
        arousal=_clamp((1.0 - energy) * 2.0 - 1.0),
        dominance=state.dominance,
        updated_at=instant.isoformat(),
    )
    _save_state(conn, state)
    behavior = describe_behavior(state)
    presence = _presence(last_user_at, instant)

    # 2. Project the current preference singleton into the fresh Wake hint.
    preference = conn.execute(
        "SELECT context_text FROM emotion_preference_state WHERE id = 1"
    ).fetchone()
    context_text = "" if preference is None else str(preference["context_text"])
    prompt = (
        f"当前情绪: valence={state.valence:.2f}, arousal={state.arousal:.2f}, "
        f"dominance={state.dominance:.2f}; presence={presence}.\n"
        f"语气约束: {behavior.tone_instruction}"
    )
    if context_text.strip():
        prompt += "\n稳定的主动偏好:\n" + context_text.strip()
    refreshed_at = instant.isoformat()
    _ = conn.execute(
        """
        INSERT INTO emotion_context_current(
            id, last_user_at, presence, prompt_section,
            source_state_updated_at, refreshed_at
        ) VALUES(1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_user_at = excluded.last_user_at,
            presence = excluded.presence,
            prompt_section = excluded.prompt_section,
            source_state_updated_at = excluded.source_state_updated_at,
            refreshed_at = excluded.refreshed_at
        """,
        (
            last_user_at.isoformat() if last_user_at is not None else None,
            presence,
            prompt,
            state.updated_at,
            refreshed_at,
        ),
    )
    conn.commit()
    return {
        "presence": presence,
        "prompt_section": prompt,
        "refreshed_at": refreshed_at,
    }


def read_fresh_context(
    path: Path,
    *,
    now: datetime,
    max_age_seconds: int,
) -> str | None:
    """Read a fresh current card without creating or mutating the database."""

    if not path.is_file() or path.is_symlink():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='emotion_context_current'"
        ).fetchone()
        if table is None:
            return None
        row = conn.execute(
            "SELECT prompt_section, refreshed_at FROM emotion_context_current WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        refreshed = _aware_datetime(datetime.fromisoformat(str(row["refreshed_at"])))
        age = (_aware_datetime(now) - refreshed).total_seconds()
        if age < 0 or age > max_age_seconds:
            return None
        prompt = str(row["prompt_section"])
        return prompt if prompt else None
    finally:
        conn.close()


def prepare_drift_proposal(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    limit: int = 10,
) -> dict[str, object] | None:
    """Create or replay one Emotion-owned ordinary Drift proposal receipt."""

    # 1. Replay a locally prepared/submitted proposal before admitting new evidence.
    existing = conn.execute(
        """
        SELECT proposal_id, revision, payload_json
        FROM emotion_drift_runs
        WHERE status IN ('prepared', 'submitted')
        ORDER BY created_at, proposal_id, revision LIMIT 1
        """
    ).fetchone()
    if existing is not None:
        return _drift_proposal_from_row(existing)

    # 2. Freeze the next bounded feedback batch and append its proposal attempt.
    state = conn.execute(
        """
        SELECT context_text, processed_feedback_sample_id
        FROM emotion_preference_state WHERE id=1
        """
    ).fetchone()
    if state is None:
        raise RuntimeError("Emotion preference singleton 缺失")
    processed = int(state["processed_feedback_sample_id"])
    rows = conn.execute(
        """
        SELECT id, created_at, feedback_type, confidence,
               user_message_id, proactive_message_id,
               user_content_preview, proactive_content_preview
        FROM emotion_feedback_samples
        WHERE id > ? AND feedback_type IN ('topic_follow', 'explicit_quote')
        ORDER BY id LIMIT ?
        """,
        (processed, limit),
    ).fetchall()
    if not rows:
        return None
    first_id = int(rows[0]["id"])
    last_id = int(rows[-1]["id"])
    proposal_id = f"emotion-feedback:{first_id}-{last_id}"
    previous = conn.execute(
        "SELECT count(1) FROM emotion_drift_runs WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    attempt = int(previous[0]) + 1 if previous is not None else 1
    revision = f"attempt-{attempt}"
    context_before = str(state["context_text"])
    payload: dict[str, object] = {
        "owner": "emotion",
        "kind": "feedback_preference_context",
        "proposal_id": proposal_id,
        "revision": revision,
        "wake_action": "select",
        "instruction": (
            "Review this bounded feedback batch, preserve only stable proactive "
            "preferences, then call emotion_commit_preference_context exactly once."
        ),
        "current_context": context_before,
        "events": [
            {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "feedback_type": str(row["feedback_type"]),
                "confidence": str(row["confidence"]),
                "user_message_id": row["user_message_id"],
                "proactive_message_id": row["proactive_message_id"],
                "user": row["user_content_preview"],
                "proactive": row["proactive_content_preview"],
            }
            for row in rows
        ],
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    created_at = _aware_utc(now)
    _ = conn.execute(
        """
        INSERT INTO emotion_drift_runs(
            proposal_id, revision, sample_first_id, sample_last_id, attempt,
            status, payload_json, context_before, created_at
        ) VALUES(?, ?, ?, ?, ?, 'prepared', ?, ?, ?)
        """,
        (
            proposal_id,
            revision,
            first_id,
            last_id,
            attempt,
            payload_json,
            context_before,
            created_at,
        ),
    )
    conn.commit()
    return {
        "proposal_id": proposal_id,
        "revision": revision,
        "payload": payload,
    }


def mark_drift_proposal_submitted(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    revision: str,
) -> None:
    """Mark the local half of an idempotently submitted Drift proposal."""

    changed = conn.execute(
        """
        UPDATE emotion_drift_runs SET status='submitted'
        WHERE proposal_id=? AND revision=? AND status='prepared'
        """,
        (proposal_id, revision),
    )
    if changed.rowcount == 0:
        row = conn.execute(
            "SELECT status FROM emotion_drift_runs WHERE proposal_id=? AND revision=?",
            (proposal_id, revision),
        ).fetchone()
        if row is None or row["status"] != "submitted":
            raise RuntimeError("Emotion Drift proposal 本地状态不一致")
    conn.commit()


def commit_drift_result(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    revision: str,
    result: Mapping[str, object],
) -> dict[str, object]:
    """Atomically append one Drift result and replace the current preference card."""

    proposal = _required_text(proposal_id, "proposal_id")
    proposal_revision = _required_text(revision, "revision")
    context_text = _required_text(result.get("context"), "context")
    result_json = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT status, sample_last_id, result_json, payload_json
            FROM emotion_drift_runs WHERE proposal_id=? AND revision=?
            """,
            (proposal, proposal_revision),
        ).fetchone()
        if row is None:
            raise ValueError("Emotion Drift proposal 不存在")
        if row["status"] == "completed":
            if str(row["result_json"]) != result_json:
                raise RuntimeError("Emotion Drift result identity conflict")
            conn.commit()
            return {"committed": False, "duplicate": True}
        if row["status"] != "submitted":
            raise RuntimeError(f"Emotion Drift proposal 不能提交: {row['status']}")
        _validate_result_evidence(result, str(row["payload_json"]))
        now = datetime.now(timezone.utc).isoformat()
        _ = conn.execute(
            """
            UPDATE emotion_drift_runs
            SET status='completed', result_json=?, completed_at=?
            WHERE proposal_id=? AND revision=?
            """,
            (result_json, now, proposal, proposal_revision),
        )
        _ = conn.execute(
            """
            UPDATE emotion_preference_state
            SET context_text=?, processed_feedback_sample_id=?, updated_at=?
            WHERE id=1
            """,
            (context_text, int(row["sample_last_id"]), now),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"committed": True, "duplicate": False}


def record_drift_turn(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    revision: str,
    session_id: str,
    turn_id: str,
    completed_at: datetime,
) -> str:
    """Record the selected Turn and expose a completed run that omitted commit."""

    row = conn.execute(
        """
        SELECT status, selected_session_id, selected_turn_id
        FROM emotion_drift_runs
        WHERE proposal_id=? AND revision=?
        """,
        (proposal_id, revision),
    ).fetchone()
    if row is None:
        raise RuntimeError("Emotion selected Drift proposal 缺少本地 receipt")
    status = str(row["status"])
    if status in {"completed", "completed_without_commit"}:
        existing = (row["selected_session_id"], row["selected_turn_id"])
        if existing == (session_id, turn_id):
            return status
        if existing != (None, None):
            raise RuntimeError("Emotion Drift selected Turn identity conflict")
    terminal = "completed" if status == "completed" else "completed_without_commit"
    if status not in {"submitted", "completed"}:
        raise RuntimeError(f"Emotion selected Drift proposal 状态无效: {status}")
    _ = conn.execute(
        """
        UPDATE emotion_drift_runs
        SET status=?, selected_session_id=?, selected_turn_id=?,
            completed_at=COALESCE(completed_at, ?)
        WHERE proposal_id=? AND revision=?
        """,
        (
            terminal,
            session_id,
            turn_id,
            _aware_utc(completed_at),
            proposal_id,
            revision,
        ),
    )
    conn.commit()
    return terminal


def _validate_result_evidence(
    result: Mapping[str, object],
    payload_json: str,
) -> None:
    """Require every committed evidence id to belong to the frozen proposal batch."""

    payload = json.loads(payload_json)
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise RuntimeError("Emotion Drift proposal payload 缺少 events")
    event_ids = {
        int(event["id"])
        for event in payload["events"]
        if isinstance(event, dict) and type(event.get("id")) is int
    }
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Emotion Drift result candidates 必须是 array")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("Emotion Drift result candidate 必须是 object")
        evidence = candidate.get("evidence")
        if not isinstance(evidence, list) or any(
            type(sample_id) is not int or sample_id not in event_ids
            for sample_id in evidence
        ):
            raise ValueError("Emotion Drift evidence 不属于冻结 proposal batch")


def _drift_proposal_from_row(row: sqlite3.Row) -> dict[str, object]:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        raise RuntimeError("Emotion Drift proposal payload 必须是 object")
    return {
        "proposal_id": str(row["proposal_id"]),
        "revision": str(row["revision"]),
        "payload": payload,
    }


def classify_feedback_delta(feedback_type: str, confidence: str) -> FeedbackDelta:
    if feedback_type == "explicit_quote":
        return FeedbackDelta(0.03, 0.08, "explicit_quote")
    if feedback_type == "topic_follow":
        if confidence in {"gold", "high"}:
            return FeedbackDelta(0.02, 0.05, "topic_follow_high")
        return FeedbackDelta(0.01, 0.03, "topic_follow_medium")
    if feedback_type == "no_topic_follow":
        return FeedbackDelta(0.0, -0.015, "no_topic_follow")
    return FeedbackDelta(0.0, 0.0, "neutral_feedback")


def apply_feedback(
    conn: sqlite3.Connection,
    *,
    source_event_id: str,
    session_key: str,
    feedback_type: str,
    confidence: str,
    payload: dict[str, Any],
) -> EmotionState:
    before = get_state(conn)
    delta = classify_feedback_delta(feedback_type, confidence)
    now = datetime.now(timezone.utc).isoformat()
    decayed = _decay(before, now)
    after = EmotionState(
        valence=_clamp(decayed.valence + delta.valence),
        arousal=decayed.arousal,
        dominance=_clamp(decayed.dominance + delta.dominance),
        updated_at=now,
    )
    try:
        _ = conn.execute(
            """
            INSERT INTO emotion_events (
                source_plugin, source_event_id, source_type, session_key,
                valence_before, arousal_before, dominance_before,
                valence_delta, arousal_delta, dominance_delta,
                valence_after, arousal_after, dominance_after,
                reason, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proactive_feedback",
                source_event_id,
                feedback_type,
                session_key,
                before.valence,
                before.arousal,
                before.dominance,
                delta.valence,
                0.0,
                delta.dominance,
                after.valence,
                after.arousal,
                after.dominance,
                delta.reason,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    except sqlite3.IntegrityError:
        return before
    if feedback_type in {"topic_follow", "explicit_quote"}:
        _insert_feedback_sample(
            conn,
            source_event_id=source_event_id,
            session_key=session_key,
            feedback_type=feedback_type,
            confidence=confidence,
            payload=payload,
        )
    _save_state(conn, after)
    conn.commit()
    return after


def _insert_feedback_sample(
    conn: sqlite3.Connection,
    *,
    source_event_id: str,
    session_key: str,
    feedback_type: str,
    confidence: str,
    payload: dict[str, Any],
) -> None:
    """Persist the bounded typed-Turn sample consumed by the Emotion Drift skill."""

    _ = conn.execute(
        """
        INSERT INTO emotion_feedback_samples (
            source_event_id, session_key, user_message_id,
            assistant_message_id, proactive_message_id, feedback_type,
            confidence, pa_score, pua_score, lag_seconds, candidate_count,
            matched_by, reason, user_content_preview,
            assistant_content_preview, proactive_content_preview
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_event_id,
            session_key,
            _payload_text(payload, "user_message_id"),
            _payload_text(payload, "assistant_message_id"),
            _payload_text(payload, "proactive_message_id"),
            feedback_type,
            confidence,
            payload.get("pa_score"),
            payload.get("pua_score"),
            payload.get("lag_seconds"),
            payload.get("candidate_count"),
            _payload_text(payload, "matched_by"),
            _payload_text(payload, "reason"),
            _payload_text(payload, "user_content_preview"),
            _payload_text(payload, "assistant_content_preview"),
            _payload_text(payload, "proactive_content_preview"),
        ),
    )


def _payload_text(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    return value if isinstance(value, str) else None


def get_state(conn: sqlite3.Connection) -> EmotionState:
    row = conn.execute(
        """
        SELECT valence, arousal, dominance, updated_at
        FROM emotion_state
        WHERE id = 1
        """
    ).fetchone()
    if row is None:
        now = datetime.now(timezone.utc).isoformat()
        return EmotionState(0.0, 0.0, 0.0, now)
    return EmotionState(
        valence=float(row["valence"]),
        arousal=float(row["arousal"]),
        dominance=float(row["dominance"]),
        updated_at=str(row["updated_at"]),
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串")
    if not value or value.strip() != value:
        raise ValueError(f"{field} 必须是无首尾空白的非空字符串")
    return value


def _save_state(conn: sqlite3.Connection, state: EmotionState) -> None:
    _ = conn.execute(
        """
        INSERT INTO emotion_state(id, valence, arousal, dominance, updated_at)
        VALUES(1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            valence = excluded.valence,
            arousal = excluded.arousal,
            dominance = excluded.dominance,
            updated_at = excluded.updated_at
        """,
        (state.valence, state.arousal, state.dominance, state.updated_at),
    )


def _decay(state: EmotionState, now_iso: str) -> EmotionState:
    try:
        before = datetime.fromisoformat(state.updated_at)
        now = datetime.fromisoformat(now_iso)
        hours = max(0.0, (now - before).total_seconds() / 3600.0)
    except Exception:
        hours = 0.0
    factor = math.exp(-hours / 72.0)
    return EmotionState(
        valence=_clamp(state.valence * factor),
        arousal=state.arousal,
        dominance=_clamp(state.dominance * factor),
        updated_at=now_iso,
    )


def _threshold_delta(dominance: float) -> float:
    if dominance >= 0.60:
        return -0.04
    if dominance >= 0.25:
        return -0.02
    if dominance <= -0.60:
        return 0.08
    if dominance <= -0.25:
        return 0.04
    return 0.0


def describe_behavior(state: EmotionState) -> EmotionBehavior:
    """把当前 VAD 状态解释为下一次主动运行会采用的行为。"""

    tone_label, tone_instruction = _tone(state)
    threshold_delta = _threshold_delta(state.dominance)
    return EmotionBehavior(
        tone_label=tone_label,
        tone_instruction=tone_instruction,
        threshold_delta=threshold_delta,
        expected_effect=_expected_effect(threshold_delta),
    )


def _tone(state: EmotionState) -> tuple[str, str]:
    if state.valence >= 0.2 and state.arousal >= 0.2 and state.dominance >= 0.1:
        return "bright_confident", "带着轻快的分享欲，但不要夸张，不要自我表演。"
    if state.valence < -0.2 and state.arousal >= 0.2:
        return "careful_tentative", "语气更谨慎克制，先确认价值，不要显得急着打扰。"
    if state.dominance <= -0.25:
        return "low_confidence", "语气保持简短、试探和低打扰感，除非内容明显重要。"
    if state.arousal <= -0.2:
        return "calm", "语气平稳放松，少用强烈表达。"
    return "neutral", "保持自然、简洁、贴近上下文的语气。"


def _expected_effect(threshold_delta: float) -> str:
    if threshold_delta > 0:
        return "raise_send_bar"
    if threshold_delta < 0:
        return "lower_send_bar"
    return "tone_only"


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Emotion timestamp 必须带时区")
    return value.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> str:
    return _aware_datetime(value).isoformat()


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Emotion last_user_at 必须是 ISO 字符串或 null")
    return _aware_datetime(datetime.fromisoformat(value))


def _presence(last_user_at: datetime | None, now: datetime) -> str:
    if last_user_at is None:
        return "unknown"
    seconds = max(0.0, (now - last_user_at).total_seconds())
    if seconds <= 30 * 60:
        return "active"
    if seconds <= 4 * 60 * 60:
        return "idle"
    return "away"
