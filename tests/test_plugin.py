from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent.plugin_composition import (
    BACKGROUND_JOBS,
    PROACTIVE_COMPONENTS,
    UI_SLOTS,
    CompositionRoot,
    PluginRuntime,
)
from agent.plugin_composition.background_jobs import (
    PluginBackgroundJobs,
    _freeze_plugin_background_jobs,
)
from agent.plugin_composition.proactive import (
    PluginProactiveComponents,
    _freeze_plugin_proactive_components,
)
from agent.plugin_composition.ui_slots import PluginUiSlots
from agent.plugins.generation_activity_host import ActivityHost
from agent.plugins.generation_proactive_bridge import CommittedProactiveBridge
from agent.plugins.generation_proactive_host import ProactiveActivityAdapter
from agent.plugins.manager import PluginManager
from agent.plugins.proactive_documents import (
    ProactiveDocumentDigests,
    ProactiveDocumentPair,
)
from bus.events_lifecycle import DriftFinished, TurnCommitted
from bus.event_bus import EventBus
from proactive_v2.frame import ProactiveFrame, ProactiveTickInput


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
async def _mount_runtime(tmp_path: Path) -> tuple[CompositionRoot, Path]:
    workspace = tmp_path / "workspace"
    emotion_root = workspace / "emotion"
    emotion_root.mkdir(parents=True)
    root = CompositionRoot("emotion-v3")
    jobs = PluginBackgroundJobs(root.instance_token)
    proactive = PluginProactiveComponents(root.instance_token)
    ui = PluginUiSlots()
    _ = await root.context.provide(BACKGROUND_JOBS, jobs)
    _ = await root.context.provide(PROACTIVE_COMPONENTS, proactive)
    _ = await root.context.provide(UI_SLOTS, ui)
    return root, emotion_root


def _copy_emotion_plugin(tmp_path: Path) -> Path:
    """Copy the plugin into an isolated discovery root for Manager tests."""

    source = Path(__file__).parents[1]
    target = tmp_path / "plugins" / "emotion"
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    return target


async def _start_emotion_manager(
    tmp_path: Path,
    plugin_dir: Path | None = None,
) -> PluginManager:
    """Boot one real Manager generation with Core's ActivityHost owner."""

    plugin_dir = plugin_dir or _copy_emotion_plugin(tmp_path)
    manager = PluginManager(
        plugin_dirs=[plugin_dir.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=tmp_path / "workspace",
        installed_cache_root=tmp_path / "cache",
    )
    manager.bind_activity_host(ActivityHost((ProactiveActivityAdapter(),)))
    await manager.load_all()
    return manager


async def _run_manager_tick(
    manager: PluginManager,
    frame: ProactiveFrame,
) -> ProactiveFrame:
    """Run one proactive module through the exact stable Root and lease."""

    snapshot = manager.current_snapshot
    activity = manager.activity_host
    if snapshot is None or activity is None:
        raise AssertionError("Manager 未发布 stable snapshot/activity")
    lease = await manager.snapshot_store.acquire(snapshot.snapshot_id)
    admission = activity.acquire(lease)
    bridge = CommittedProactiveBridge(activity)
    token = bridge.bind_execution(lease)
    try:
        runtime = bridge.runtime_for(snapshot)
        modules = bridge.lifecycle_modules(
            runtime,
            lifecycle_id="default.proactive.frame.v1",
        )
        if len(modules) != 1:
            raise AssertionError(f"Emotion proactive module 数量异常: {len(modules)}")
        return await cast(Any, modules[0]).run(frame)
    finally:
        bridge.reset_execution(token)
        await admission.release()
        await lease.release()


def test_module_exports_pure_v3_contract() -> None:
    assert module.api_version == 3
    assert module.name == "emotion"
    assert module.version == "3.0.0"
    assert inspect.signature(module.apply).parameters.keys() == {"ctx", "config"}
    module_file = module.__file__
    assert isinstance(module_file, str)
    source = Path(module_file).read_text(encoding="utf-8")
    assert "class EmotionPlugin" not in source
    assert "ProactiveFeedbackRecorded" not in source
    assert "EventBus" not in source
    assert "from agent.plugins import" not in source


@pytest.mark.asyncio
async def test_v3_apply_freezes_all_catalogs_without_opening_db(tmp_path: Path) -> None:
    root, emotion_root = await _mount_runtime(tmp_path)
    _ = await root.mount(
        lambda ctx: module.apply(ctx, object()),
        name="emotion",
        inject=(BACKGROUND_JOBS, PROACTIVE_COMPONENTS, UI_SLOTS),
        runtime=PluginRuntime(
            plugin_id="emotion",
            plugin_dir=Path(__file__).parents[1],
            data_dir=tmp_path / "plugin-data",
            workspace=emotion_root.parent,
            config=None,
            workspace_roots=("emotion",),
        ),
    )
    jobs = root.context.get(BACKGROUND_JOBS)
    proactive = root.context.get(PROACTIVE_COMPONENTS)
    assert jobs is not None and proactive is not None
    job_catalog = _freeze_plugin_background_jobs(jobs, root.instance_token)
    proactive_catalog = _freeze_plugin_proactive_components(
        proactive,
        root.instance_token,
    )
    assert job_catalog.job("emotion:merge_proactive_pending") is not None
    module_binding = proactive_catalog.module("emotion:proactive.prompt.emotion")
    assert module_binding is not None
    assert module_binding.definition.domain_effect == "emotion.state"
    assert not (emotion_root / "emotion.db").exists()
    await root.dispose()


@pytest.mark.asyncio
async def test_typed_turn_feedback_is_idempotent_and_mobile_read_only(
    tmp_path: Path,
) -> None:
    root, emotion_root = await _mount_runtime(tmp_path)
    _ = await root.mount(
        lambda ctx: module.apply(ctx, object()),
        name="emotion",
        inject=(BACKGROUND_JOBS, PROACTIVE_COMPONENTS, UI_SLOTS),
        runtime=PluginRuntime(
            plugin_id="emotion",
            plugin_dir=Path(__file__).parents[1],
            data_dir=tmp_path / "plugin-data",
            workspace=emotion_root.parent,
            config=None,
            workspace_roots=("emotion",),
        ),
    )
    event = TurnCommitted(
        session_key="mobile:test",
        channel="test",
        chat_id="chat",
        input_message="被回复消息：主动提醒某个主题\n\n【你当前新消息】继续这个主题",
        persisted_user_message="被回复消息：主动提醒某个主题\n\n【你当前新消息】继续这个主题",
        assistant_response="继续回答",
        tools_used=[],
        turn_id="turn-1",
        persisted_user_message_id="u1",
        assistant_message_id="a1",
    )
    module._on_turn_committed(event)
    module._on_turn_committed(event)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
    bootstrap = module._mobile_ui_query(
        "emotion.bootstrap",
        {"limit": 10},
        session_id=None,
        turn_id=None,
    )
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
    assert before == after
    assert bootstrap["overview"]["event_count"] == 1
    assert bootstrap["overview"]["influence_count"] == 1
    assert bootstrap["items"][0]["source_type"] == "explicit_quote"
    await root.dispose()


@pytest.mark.asyncio
async def test_proactive_projection_requires_and_uses_domain_effect_facade(
    tmp_path: Path,
) -> None:
    emotion_root = tmp_path / "emotion"
    emotion_root.mkdir()
    projection = module.EmotionProjectionModule(emotion_root)
    frame = ProactiveFrame(
        input=ProactiveTickInput(
            session_key="proactive:test",
            started_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
    )
    calls: list[str] = []

    class Effects:
        async def run(self, effect_id: str, transaction):
            calls.append(effect_id)
            effect_context = SimpleNamespace(
                semantic_job_id="emotion:proactive.prompt.emotion",
                event_id="proactive:test:2026-08-17T00:00:00+00:00",
                invocation_id=(
                    "proactive:emotion:proactive.prompt.emotion:"
                    "proactive:test:2026-08-17T00:00:00+00:00"
                ),
                effect_id=effect_id,
                idempotency_key=(
                    "proactive:test:2026-08-17T00:00:00+00:00:"
                    "emotion:proactive.prompt.emotion"
                ),
                attempt=1,
                tick_id="proactive:test:2026-08-17T00:00:00+00:00",
            )
            result = transaction(effect_context)
            if inspect.isawaitable(result):
                await result
            return object()

    result = await projection.run(SimpleNamespace(domain_effects=Effects()), frame)
    assert result.slots["proactive:prompt:system_bottom:emotion"]
    assert result.slots["proactive:effect:emotion"]["metadata"]["expected_effect"] == "tone_only"
    assert calls == ["emotion.state"]
    db = module.open_db(emotion_root / "emotion.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM emotion_effects").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0] == 1
    finally:
        db.close()
    with pytest.raises(RuntimeError, match="domain effects facade"):
        await projection.run(SimpleNamespace(domain_effects=None), frame)


@pytest.mark.asyncio
async def test_manager_proactive_failure_rolls_back_then_retries_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _start_emotion_manager(tmp_path)
    try:
        generation = manager.generation("emotion")
        assert generation is not None
        plugin_module = cast(Any, generation.instance).module
        frame = ProactiveFrame(
            input=ProactiveTickInput(
                session_key="proactive:emotion",
                started_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
        )
        original_build_effect = plugin_module.build_effect

        def fail_after_writes(conn, **kwargs):
            result = original_build_effect(conn, **kwargs)
            raise RuntimeError("synthetic precommit failure")

        monkeypatch.setattr(plugin_module, "build_effect", fail_after_writes)
        with pytest.raises(RuntimeError, match="synthetic precommit failure"):
            await _run_manager_tick(manager, frame)

        db = module.open_db(tmp_path / "workspace" / "emotion" / "emotion.db")
        try:
            assert db.execute("SELECT COUNT(*) FROM emotion_effects").fetchone()[0] == 0
            assert (
                db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0]
                == 0
            )
            state = db.execute(
                "SELECT valence, arousal, dominance FROM emotion_state WHERE id = 1"
            ).fetchone()
            assert state is not None and tuple(state) == (0.0, 0.0, 0.0)
        finally:
            db.close()

        monkeypatch.setattr(plugin_module, "build_effect", original_build_effect)
        first = await _run_manager_tick(manager, frame)
        second = await _run_manager_tick(manager, frame)
        assert first.slots["proactive:effect:emotion"] == second.slots[
            "proactive:effect:emotion"
        ]

        db = module.open_db(tmp_path / "workspace" / "emotion" / "emotion.db")
        try:
            assert db.execute("SELECT COUNT(*) FROM emotion_effects").fetchone()[0] == 1
            assert (
                db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0]
                == 1
            )
            tick_id = "proactive:emotion:2026-08-17T00:00:00+00:00"
            receipt = db.execute(
                """
                SELECT semantic_job_id, event_id, invocation_id, effect_id,
                       idempotency_key, attempt
                FROM emotion_domain_effects
                """
            ).fetchone()
            assert receipt is not None
            assert tuple(receipt) == (
                "emotion:proactive.prompt.emotion",
                tick_id,
                f"proactive:emotion:proactive.prompt.emotion:{tick_id}",
                "emotion.state",
                f"{tick_id}:emotion:proactive.prompt.emotion",
                1,
            )
        finally:
            db.close()
    finally:
        await manager.terminate_all()


@pytest.mark.asyncio
async def test_manager_proactive_commit_survives_cancellation_and_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _start_emotion_manager(tmp_path)
    try:
        from agent.plugins.generation_job_host import ProactiveDomainEffects

        frame = ProactiveFrame(
            input=ProactiveTickInput(
                session_key="proactive:emotion-cancel",
                started_at=datetime(2026, 8, 17, 0, 1, tzinfo=timezone.utc),
            )
        )
        original_lookup = ProactiveDomainEffects._lookup_committed
        lookup_calls = 0

        async def cancel_after_commit(self):
            nonlocal lookup_calls
            lookup_calls += 1
            record = await original_lookup(self)
            if lookup_calls == 2:
                raise asyncio.CancelledError
            return record

        monkeypatch.setattr(
            ProactiveDomainEffects,
            "_lookup_committed",
            cancel_after_commit,
        )
        with pytest.raises(asyncio.CancelledError):
            await _run_manager_tick(manager, frame)
        assert lookup_calls == 2

        monkeypatch.setattr(
            ProactiveDomainEffects,
            "_lookup_committed",
            original_lookup,
        )
        resumed = await _run_manager_tick(manager, frame)
        assert resumed.slots["proactive:prompt:system_bottom:emotion"]

        db = module.open_db(tmp_path / "workspace" / "emotion" / "emotion.db")
        try:
            assert db.execute("SELECT COUNT(*) FROM emotion_effects").fetchone()[0] == 1
            assert (
                db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0]
                == 1
            )
        finally:
            db.close()
    finally:
        await manager.terminate_all()


def test_manager_proactive_receipt_survives_core_process_crash_and_reentry(
    tmp_path: Path,
) -> None:
    plugin_dir = _copy_emotion_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    script = """
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.plugins.generation_activity_host import ActivityHost
from agent.plugins.generation_proactive_bridge import CommittedProactiveBridge
from agent.plugins.generation_proactive_host import ProactiveActivityAdapter
from agent.plugins.generation_job_host import ProactiveDomainEffects
from agent.plugins.manager import PluginManager
from bus.event_bus import EventBus
from proactive_v2.frame import ProactiveFrame, ProactiveTickInput


async def run_tick(manager, frame):
    snapshot = manager.current_snapshot
    activity = manager.activity_host
    assert snapshot is not None and activity is not None
    lease = await manager.snapshot_store.acquire(snapshot.snapshot_id)
    admission = activity.acquire(lease)
    bridge = CommittedProactiveBridge(activity)
    token = bridge.bind_execution(lease)
    try:
        runtime = bridge.runtime_for(snapshot)
        modules = bridge.lifecycle_modules(
            runtime,
            lifecycle_id="default.proactive.frame.v1",
        )
        assert len(modules) == 1
        await modules[0].run(frame)
    finally:
        bridge.reset_execution(token)
        await admission.release()
        await lease.release()


async def main():
    plugin_parent = Path(sys.argv[1])
    workspace = Path(sys.argv[2])
    manager = PluginManager(
        plugin_dirs=[plugin_parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=workspace.parent / "cache",
    )
    manager.bind_activity_host(ActivityHost((ProactiveActivityAdapter(),)))
    original_lookup = ProactiveDomainEffects._lookup_committed
    lookup_calls = 0

    async def crash_after_commit(self):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 2:
            os._exit(137)
        return await original_lookup(self)

    ProactiveDomainEffects._lookup_committed = crash_after_commit
    await manager.load_all()
    frame = ProactiveFrame(
        input=ProactiveTickInput(
            session_key="proactive:emotion-crash",
            started_at=datetime(2026, 8, 17, 0, 2, tzinfo=timezone.utc),
        )
    )
    await run_tick(manager, frame)


asyncio.run(main())
"""
    env = dict(os.environ)
    core_root = os.environ.get("AKASHIC_AGENT_ROOT") or str(
        Path(__file__).parents[3] / "akasic-agent"
    )
    env["PYTHONPATH"] = core_root + os.pathsep + env.get("PYTHONPATH", "")
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(plugin_dir.parent),
            str(workspace),
        ],
        env=env,
        check=False,
    )
    assert crashed.returncode == 137

    db = module.open_db(workspace / "emotion" / "emotion.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM emotion_effects").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0]
            == 1
        )
    finally:
        db.close()

    async def reenter() -> None:
        manager = await _start_emotion_manager(tmp_path, plugin_dir)
        try:
            frame = ProactiveFrame(
                input=ProactiveTickInput(
                    session_key="proactive:emotion-crash",
                    started_at=datetime(2026, 8, 17, 0, 2, tzinfo=timezone.utc),
                )
            )
            resumed = await _run_manager_tick(manager, frame)
            assert resumed.slots["proactive:prompt:system_bottom:emotion"]
        finally:
            await manager.terminate_all()

    asyncio.run(reenter())
    db = module.open_db(workspace / "emotion" / "emotion.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM emotion_effects").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0]
            == 1
        )
    finally:
        db.close()


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


def test_domain_effect_receipt_survives_core_process_crash_and_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "emotion" / "emotion.db"
    plugin_path = Path(__file__).parents[1] / "plugin.py"
    script = """
import importlib.util
import os
import sys
import types
from pathlib import Path

path = Path(sys.argv[1])
package_name = "emotion_crash_test"
package = types.ModuleType(package_name)
package.__path__ = [str(path.parent)]
sys.modules[package_name] = package
spec = importlib.util.spec_from_file_location(
    package_name + ".plugin",
    path,
    submodule_search_locations=[str(path.parent)],
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
conn = module.open_db(Path(sys.argv[2]))
module.commit_domain_effect(
    conn,
    semantic_job_id="emotion:merge_proactive_pending",
    event_id="crash-event",
    invocation_id="crash-invocation",
    effect_id="emotion.state",
    idempotency_key="emotion:merge_proactive_pending:event:crash-event",
    attempt=1,
    result_digest="crash-digest",
)
conn.close()
os._exit(137)
    """
    env = dict(os.environ)
    core_root = os.environ.get("AKASHIC_AGENT_ROOT") or str(
        Path(__file__).parents[3] / "akasic-agent"
    )
    env["PYTHONPATH"] = core_root + os.pathsep + str(plugin_path.parent)
    result = subprocess.run(
        [sys.executable, "-c", script, str(plugin_path), str(db_path)],
        env=env,
        check=False,
    )
    assert result.returncode == 137
    restarted = module.open_db(db_path)
    try:
        found = module.lookup_domain_effect(
            restarted,
            invocation_id="crash-invocation",
            effect_id="emotion.state",
            idempotency_key="emotion:merge_proactive_pending:event:crash-event",
        )
    finally:
        restarted.close()
    assert found is not None
    assert found.result_digest == "crash-digest"


@pytest.mark.asyncio
async def test_v3_apply_registers_job_without_opening_emotion_db(tmp_path: Path) -> None:
    root, emotion_root = await _mount_runtime(tmp_path)

    _ = await root.mount(
        lambda ctx: module.apply(ctx, object()),
        name="emotion",
        inject=(BACKGROUND_JOBS, PROACTIVE_COMPONENTS, UI_SLOTS),
        runtime=PluginRuntime(
            plugin_id="emotion",
            plugin_dir=Path(__file__).parents[1],
            data_dir=tmp_path / "plugin-data",
            workspace=emotion_root.parent,
            config=None,
            workspace_roots=("emotion",),
        ),
    )
    jobs = root.context.get(BACKGROUND_JOBS)
    assert jobs is not None
    catalog = _freeze_plugin_background_jobs(jobs, root.instance_token)
    binding = catalog.job("emotion:merge_proactive_pending")
    assert binding is not None
    assert binding.definition.documents_scope == ("emotion",)
    assert binding.definition.domain_effect == "emotion.state"
    assert not (emotion_root / "emotion.db").exists()
    await root.dispose()


@pytest.mark.asyncio
async def test_v3_merge_uses_core_ports_and_durable_emotion_receipt(
    tmp_path: Path,
) -> None:
    emotion_root = tmp_path / "workspace" / "emotion"
    emotion_root.mkdir(parents=True)
    setattr(module, "_v3_emotion_root", emotion_root)
    calls: list[str] = []
    prepared_intent = object()
    issued_receipt = object()

    class Documents:
        def read_pair(self):
            calls.append("read")
            return (
                ProactiveDocumentDigests(context=None, pending=None),
                ProactiveDocumentPair(
                    context=b"# Proactive Context\n",
                    pending=b"- [ ] prefer calm summaries\n",
                ),
            )

        async def prepare_pair(self, expected, pair):
            calls.append("prepare")
            assert expected.pending is None
            assert pair["pending"] == b""
            return prepared_intent

        async def commit_after(self, intent, receipt):
            calls.append("documents")
            assert intent is prepared_intent
            assert receipt is issued_receipt
            return object()

    class Effects:
        async def run(self, effect_id, transaction):
            calls.append("effect")
            effect_ctx = SimpleNamespace(
                semantic_job_id="emotion:merge_proactive_pending",
                event_id="drift-v3-1",
                invocation_id="invocation-v3-1",
                effect_id=effect_id,
                idempotency_key="emotion:merge_proactive_pending:event:drift-v3-1",
                attempt=1,
            )
            await transaction(effect_ctx)
            durable = module.lookup_emotion_domain_effect_v3(effect_ctx)
            assert durable is not None
            return issued_receipt

    class Llm:
        async def generate_text(self, **kwargs):
            calls.append("llm")
            assert "prefer calm summaries" in kwargs["prompt"]
            return "# Proactive Context\n\n- Prefer calm summaries."

    event = DriftFinished(
        event_id="drift-v3-1",
        session_key="session",
        skill_name="feedback-preference-context",
        status="completed",
        briefing="briefing",
        message_result="ok",
        timestamp=datetime.now(timezone.utc),
    )
    ctx = SimpleNamespace(
        event=event,
        documents=Documents(),
        domain_effects=Effects(),
        llm=Llm(),
    )

    await module.merge_proactive_pending_v3(ctx)

    assert calls == ["read", "llm", "prepare", "effect", "documents"]
    db = module.open_db(emotion_root / "emotion.db")
    try:
        assert db.execute("SELECT COUNT(*) FROM emotion_domain_effects").fetchone()[0] == 1
    finally:
        db.close()
