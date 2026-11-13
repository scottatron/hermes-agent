"""Behavior of gateway task publication and reload."""
import asyncio
from contextlib import asynccontextmanager

from hermes_cli.plugin_gateway_tasks import clear_host, publish_host
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def test_gateway_task_load_reload_and_shutdown(tmp_path, monkeypatch):
    async def scenario():
        import gateway.run
        seen = []
        @asynccontextmanager
        async def scope(home):
            seen.append(("scope", home))
            yield
        monkeypatch.setattr(gateway.run, "_async_profile_runtime_scope", scope)
        manager = PluginManager(scope_key=str(tmp_path))
        manifest = PluginManifest(name="probe", key="probe")
        first = PluginContext(manifest, manager)
        started = asyncio.Event()
        release = asyncio.Event()
        async def old():
            seen.append("old")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
                seen.append("old stopped")
        first.register_gateway_task(old, name="worker")
        owner = object()
        publish_host(owner, asyncio.get_running_loop())
        await asyncio.sleep(0)
        assert seen == []  # unpublished registration, like a failed plugin load
        manager._publish_staged_registrations(first)
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.to_thread(manager.unload)
        second = PluginContext(manifest, manager)
        async def replacement():
            seen.append("new")
            await asyncio.Event().wait()
        second.register_gateway_task(replacement, name="worker")
        await asyncio.to_thread(manager._publish_staged_registrations, second)
        await asyncio.sleep(0.01)  # health: allow HX013 -- wait to prove the blocked worker cannot start
        assert "new" not in seen
        await asyncio.to_thread(manager.unload)
        third = PluginContext(manifest, manager)
        third_started = asyncio.Event()
        async def newest():
            seen.append("newest")
            third_started.set()
            await asyncio.Event().wait()
        third.register_gateway_task(newest, name="worker")
        await asyncio.to_thread(manager._publish_staged_registrations, third)
        await asyncio.sleep(0.01)  # health: allow HX013 -- wait to prove the blocked worker cannot start
        assert "newest" not in seen
        release.set()
        await asyncio.wait_for(third_started.wait(), 2)
        assert "new" not in seen
        assert seen.index("old stopped") < seen.index("newest")
        assert ("scope", tmp_path.resolve()) in seen
        await clear_host(object())
        assert not second._staged
        await clear_host(owner)
        manager.unload()
    asyncio.run(scenario())


def test_gateway_task_disposed_before_host_never_starts(tmp_path):
    async def scenario():
        manager = PluginManager(scope_key=str(tmp_path))
        context = PluginContext(PluginManifest(name="probe", key="probe"), manager)
        called = []
        async def worker():
            called.append(True)
        context.register_gateway_task(worker)
        manager._publish_staged_registrations(context)
        manager.unload()
        owner = object()
        publish_host(owner, asyncio.get_running_loop())
        await asyncio.sleep(0)
        assert not called
        await clear_host(owner)
    asyncio.run(scenario())


def test_shutdown_fences_new_owner_until_all_generations_stop(tmp_path, monkeypatch):
    import pytest
    import gateway.run
    import hermes_cli.plugin_gateway_tasks as lifecycle
    @asynccontextmanager
    async def scope(_home):
        yield
    monkeypatch.setattr(gateway.run, "_async_profile_runtime_scope", scope)
    monkeypatch.setattr(lifecycle, "_DRAIN_TIMEOUT", 0.01)
    manager = PluginManager(scope_key=str(tmp_path))
    manifest = PluginManifest(name="probe", key="probe")
    release = None
    async def first_loop():
        nonlocal release
        owner = object()
        release = asyncio.Event()
        started = asyncio.Event()
        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
        ctx = PluginContext(manifest, manager)
        ctx.register_gateway_task(worker, name="worker")
        manager._publish_staged_registrations(ctx)
        publish_host(owner, asyncio.get_running_loop())
        await asyncio.wait_for(started.wait(), 2)
        manager.unload()
        await clear_host(owner)
        with pytest.raises(RuntimeError):
            publish_host(object(), asyncio.get_running_loop())
        release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)  # health: allow HX013 -- wait to prove the blocked worker cannot start
            if lifecycle._host is None:
                break
        assert lifecycle._host is None
    asyncio.run(first_loop())
    async def second_loop():
        owner = object()
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        ctx = PluginContext(manifest, manager)
        ctx.register_gateway_task(worker, name="worker")
        manager._publish_staged_registrations(ctx)
        publish_host(owner, asyncio.get_running_loop())
        await asyncio.wait_for(started.wait(), 2)
        manager.unload()
        replacement = PluginContext(manifest, manager)
        replacement_started = asyncio.Event()
        async def next_worker():
            replacement_started.set()
        replacement.register_gateway_task(next_worker, name="worker")
        manager._publish_staged_registrations(replacement)
        await asyncio.wait_for(replacement_started.wait(), 2)
        assert stopped.is_set()
        await clear_host(owner)
        manager.unload()
    asyncio.run(second_loop())


def test_gateway_tasks_bind_each_profile(tmp_path, monkeypatch):
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home
    monkeypatch.setenv("TASK_TEST_SECRET", "ambient")
    homes = [tmp_path / "one", tmp_path / "two"]
    for home in homes:
        home.mkdir()
        (home / ".env").write_text(f"TASK_TEST_SECRET={home.name}\n")
    async def scenario():
        owner = object()
        seen = {}
        managers = []
        for home in homes:
            manager = PluginManager(scope_key=str(home))
            managers.append(manager)
            ctx = PluginContext(PluginManifest(name="probe", key="probe"), manager)
            async def worker(expected=home):
                await asyncio.sleep(0)
                seen[expected] = (get_hermes_home(), get_secret("TASK_TEST_SECRET"))
            ctx.register_gateway_task(worker, name="worker")
            manager._publish_staged_registrations(ctx)
        publish_host(owner, asyncio.get_running_loop())
        for _ in range(100):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.01)  # health: allow HX013 -- wait to prove the blocked worker cannot start
        assert seen == {home: (home, home.name) for home in homes}
        await clear_host(owner)
        for manager in managers:
            manager.unload()
    asyncio.run(scenario())
