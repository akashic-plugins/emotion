from __future__ import annotations

import json
import math
import sqlite3
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


@dataclass(frozen=True)
class EmotionDomainEffect:
    """表示一次已由 Emotion SQLite 提交的幂等领域效果。"""

    semantic_job_id: str
    event_id: str
    invocation_id: str
    effect_id: str
    idempotency_key: str
    attempt: int
    result_digest: str


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ = conn.execute("PRAGMA journal_mode = WAL")
    _ = conn.execute("PRAGMA synchronous = NORMAL")
    _ = conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS emotion_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            valence REAL NOT NULL,
            arousal REAL NOT NULL,
            dominance REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS emotion_events (
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

        CREATE TABLE IF NOT EXISTS emotion_feedback_samples (
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
        );

        CREATE TABLE IF NOT EXISTS emotion_effects (
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

        CREATE TABLE IF NOT EXISTS emotion_domain_effects (
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
        );
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    _ = conn.execute(
        """
        INSERT OR IGNORE INTO emotion_state(id, valence, arousal, dominance, updated_at)
        VALUES(1, 0.0, 0.0, 0.0, ?)
        """,
        (now,),
    )
    conn.commit()
    return conn


def commit_domain_effect(
    conn: sqlite3.Connection,
    *,
    semantic_job_id: str,
    event_id: str,
    invocation_id: str,
    effect_id: str,
    idempotency_key: str,
    attempt: int,
    result_digest: str,
) -> EmotionDomainEffect:
    """在 Emotion 事务内幂等提交一次 job 领域效果及其 durable receipt。"""

    effect = EmotionDomainEffect(
        semantic_job_id=_required_text(semantic_job_id, "semantic_job_id"),
        event_id=_required_text(event_id, "event_id"),
        invocation_id=_required_text(invocation_id, "invocation_id"),
        effect_id=_required_text(effect_id, "effect_id"),
        idempotency_key=_required_text(idempotency_key, "idempotency_key"),
        attempt=_required_attempt(attempt),
        result_digest=_required_text(result_digest, "result_digest"),
    )

    # 1. 锁定同一语义事件，避免新 invocation 重复提交领域效果。
    #    若调用方已经开启事务，receipt 必须加入该事务，不能提前提交。
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM emotion_domain_effects
            WHERE semantic_job_id = ? AND event_id = ? AND effect_id = ?
            """,
            (effect.semantic_job_id, effect.event_id, effect.effect_id),
        ).fetchone()
        if row is not None:
            existing = _domain_effect_from_row(row)
            if existing != effect:
                raise RuntimeError("Emotion domain effect 幂等 identity 漂移")
            if owns_transaction:
                conn.commit()
            return existing

        # 2. receipt 与该 effect 的领域写集在同一 SQLite transaction 提交。
        conn.execute(
            """
            INSERT INTO emotion_domain_effects (
                semantic_job_id, event_id, invocation_id, effect_id,
                idempotency_key, attempt, result_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                effect.semantic_job_id,
                effect.event_id,
                effect.invocation_id,
                effect.effect_id,
                effect.idempotency_key,
                effect.attempt,
                effect.result_digest,
            ),
        )
        if owns_transaction:
            conn.commit()
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise
    return effect


def lookup_domain_effect(
    conn: sqlite3.Connection,
    *,
    invocation_id: str,
    effect_id: str,
    idempotency_key: str,
) -> EmotionDomainEffect | None:
    """按 Core 固定的 invocation identity 读取 Emotion durable receipt。"""

    row = conn.execute(
        """
        SELECT * FROM emotion_domain_effects
        WHERE invocation_id = ? AND effect_id = ? AND idempotency_key = ?
        """,
        (
            _required_text(invocation_id, "invocation_id"),
            _required_text(effect_id, "effect_id"),
            _required_text(idempotency_key, "idempotency_key"),
        ),
    ).fetchone()
    return None if row is None else _domain_effect_from_row(row)


def lookup_domain_effect_path(
    path: Path,
    *,
    invocation_id: str,
    effect_id: str,
    idempotency_key: str,
) -> EmotionDomainEffect | None:
    """只读查询既有 Emotion DB，不因恢复扫描创建任何文件。"""

    if not path.is_file() or path.is_symlink():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return lookup_domain_effect(
            conn,
            invocation_id=invocation_id,
            effect_id=effect_id,
            idempotency_key=idempotency_key,
        )
    finally:
        conn.close()


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


def build_effect(
    conn: sqlite3.Connection,
    *,
    tick_id: str,
    session_key: str,
    now_utc: datetime,
    last_user_at: datetime | None,
    base_threshold: float,
    commit: bool = True,
) -> dict[str, Any]:
    stored = _decay(get_state(conn), now_utc.isoformat())
    energy = compute_energy(last_user_at, now_utc)
    arousal = _clamp((1.0 - energy) * 2.0 - 1.0)
    state = EmotionState(
        valence=stored.valence,
        arousal=arousal,
        dominance=stored.dominance,
        updated_at=now_utc.isoformat(),
    )
    _save_state(conn, state)
    behavior = describe_behavior(state)
    tone_label = behavior.tone_label
    tone_instruction = behavior.tone_instruction
    threshold_delta = behavior.threshold_delta
    final_threshold = _clamp_threshold(base_threshold + threshold_delta)
    expected_effect = behavior.expected_effect
    prompt_section = (
        f"当前 VAD: valence={state.valence:.2f}, arousal={state.arousal:.2f}, dominance={state.dominance:.2f}。\n"
        f"语气约束: {tone_instruction}\n"
        f"发送克制程度: base_threshold={base_threshold:.2f}, effective_threshold={final_threshold:.2f}。"
        " effective_threshold 越高，越需要确认内容确实值得打扰用户。"
    )
    metadata: dict[str, object] = {
        "valence": round(state.valence, 4),
        "arousal": round(state.arousal, 4),
        "dominance": round(state.dominance, 4),
        "base_threshold": round(base_threshold, 4),
        "final_threshold": round(final_threshold, 4),
        "tone_label": tone_label,
        "expected_effect": expected_effect,
    }
    _ = conn.execute(
        """
        INSERT INTO emotion_effects (
            tick_id, session_key, valence, arousal, dominance,
            base_threshold, final_threshold, threshold_delta,
            tone_label, expected_effect, prompt_section, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tick_id) DO UPDATE SET
            session_key = excluded.session_key,
            valence = excluded.valence,
            arousal = excluded.arousal,
            dominance = excluded.dominance,
            base_threshold = excluded.base_threshold,
            final_threshold = excluded.final_threshold,
            threshold_delta = excluded.threshold_delta,
            tone_label = excluded.tone_label,
            expected_effect = excluded.expected_effect,
            prompt_section = excluded.prompt_section,
            metadata_json = excluded.metadata_json
        """,
        (
            tick_id,
            session_key,
            state.valence,
            state.arousal,
            state.dominance,
            base_threshold,
            final_threshold,
            threshold_delta,
            tone_label,
            expected_effect,
            prompt_section,
            json.dumps(metadata, ensure_ascii=False),
        ),
    )
    if commit:
        conn.commit()
    return {
        "provider_name": "emotion",
        "prompt_section": prompt_section,
        "threshold_delta": threshold_delta,
        "metadata": metadata,
    }


def lookup_effect(
    conn: sqlite3.Connection,
    *,
    tick_id: str,
) -> dict[str, Any] | None:
    """Read one previously committed proactive projection without recomputing it."""

    row = conn.execute(
        """
        SELECT prompt_section, threshold_delta, metadata_json
        FROM emotion_effects
        WHERE tick_id = ?
        """,
        (tick_id,),
    ).fetchone()
    if row is None:
        return None
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata, dict):
        raise RuntimeError("Emotion effect metadata 必须是 object")
    return {
        "provider_name": "emotion",
        "prompt_section": str(row["prompt_section"]),
        "threshold_delta": float(row["threshold_delta"]),
        "metadata": metadata,
    }


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


def _domain_effect_from_row(row: sqlite3.Row) -> EmotionDomainEffect:
    return EmotionDomainEffect(
        semantic_job_id=str(row["semantic_job_id"]),
        event_id=str(row["event_id"]),
        invocation_id=str(row["invocation_id"]),
        effect_id=str(row["effect_id"]),
        idempotency_key=str(row["idempotency_key"]),
        attempt=int(row["attempt"]),
        result_digest=str(row["result_digest"]),
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串")
    if not value or value.strip() != value:
        raise ValueError(f"{field} 必须是无首尾空白的非空字符串")
    return value


def _required_attempt(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("attempt 必须是整数")
    if value < 1:
        raise ValueError("attempt 必须是正整数")
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


def _clamp_threshold(value: float) -> float:
    return max(0.54, min(0.78, value))
