"""Isolated contract tests for the proposed native Slack reaction patch."""
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import hermes_cli.platform_actions as pa

class Client:
    def __init__(self, error=None):
        self.reactions_add = AsyncMock()
        if error:
            self.reactions_add.side_effect = error


class SlackError(Exception):
    def __init__(self, response):
        self.response = response


class Adapter:
    platform = types.SimpleNamespace(value="slack")
    is_connected = True


def source(**updates):
    value = dict(platform="slack", chat_id="C1", scope_id="T1", user_id="U1",
                 thread_id="171.1", profile="p1")
    value.update(updates)
    return value


def setup_resolution(monkeypatch, adapter, profile="p1"):
    owner = profile
    runner = types.SimpleNamespace(
        _delivery_adapter_for=lambda src: adapter,
        _owning_profile=lambda resolved, platform: (resolved is adapter, owner),
        _primary_profile_name="default",
    )
    mods = {
        "hermes_cli": types.ModuleType("hermes_cli"),
        "hermes_cli.profiles": types.SimpleNamespace(get_active_profile_name=lambda: profile),
        "gateway": types.ModuleType("gateway"),
        "gateway.config": types.SimpleNamespace(Platform=types.SimpleNamespace(SLACK="slack")),
        "gateway.run": types.SimpleNamespace(_gateway_runner_ref=lambda: runner),
        "gateway.session": types.SimpleNamespace(SessionSource=lambda **kw: types.SimpleNamespace(**kw)),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return runner


@pytest.mark.asyncio
async def test_explicit_workspace_client_and_exact_target(monkeypatch):
    adapter = Adapter()
    adapter._team_clients = {"T1": Client(), "T2": Client()}
    captured = []
    runner = setup_resolution(monkeypatch, adapter)
    runner._delivery_adapter_for = lambda src: (captured.append(src) or adapter)
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    result = await action.add_reaction("slack", "C1", "171.2", "eyes", source=source())
    assert result == {"ok": True, "action": "add_reaction"}
    adapter._team_clients["T1"].reactions_add.assert_awaited_once_with(channel="C1", timestamp="171.2", name="eyes")
    adapter._team_clients["T2"].reactions_add.assert_not_awaited()
    assert (captured[0].scope_id, captured[0].user_id, captured[0].thread_id, captured[0].profile) == (
        "T1", "U1", "171.1", "p1")


@pytest.mark.asyncio
async def test_missing_source_mismatch_and_no_grant_fail_closed(monkeypatch):
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    assert (await action.add_reaction("slack", "C1", "1", ":eyes:")).get("error") == "invalid_argument"
    adapter = Adapter()
    adapter._team_clients = {"T1": Client()}
    setup_resolution(monkeypatch, adapter)
    assert (await action.add_reaction("slack", "C2", "1", ":eyes:", source=source())).get("error") == "invalid_argument"
    assert not adapter._team_clients["T1"].reactions_add.await_count
    monkeypatch.setattr(action, "_capability_granted", lambda: False)
    assert (await action.add_reaction("slack", "C1", "1", ":eyes:", source=source())).get("error") == "capability_not_granted"


@pytest.mark.asyncio
async def test_profile_mismatch_route_miss_and_unknown_workspace(monkeypatch):
    adapter = Adapter()
    adapter._team_clients = {"T1": Client()}
    setup_resolution(monkeypatch, adapter, profile="active")
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    assert (await action.add_reaction("slack", "C1", "1", ":eyes:", source=source())).get("error") == "invalid_argument"
    runner = setup_resolution(monkeypatch, None, profile="p1")
    assert (await action.add_reaction("slack", "C1", "1", ":eyes:", source=source())).get("error") == "adapter_not_registered"
    setup_resolution(monkeypatch, adapter)
    assert (await action.add_reaction("slack", "C1", "1", ":eyes:", source=source(scope_id="UNKNOWN"))).get("error") == "adapter_not_registered"
    assert not adapter._team_clients["T1"].reactions_add.await_count


@pytest.mark.asyncio
async def test_top_level_message_disconnected_resolver_errors_and_profile_fail_closed(monkeypatch):
    adapter = Adapter()
    adapter._team_clients = {"T1": Client(), "T0": Client()}
    seen = []
    runner = setup_resolution(monkeypatch, adapter)
    runner._delivery_adapter_for = lambda src: (seen.append(src) or adapter)
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    result = await action.add_reaction("slack", "C1", "root-ts", "eyes", source=source(thread_id=None))
    assert result["ok"] is True
    assert seen[-1].thread_id is None and seen[-1].chat_type == "channel"
    assert (await action.add_reaction("slack", "C1", "m", "eyes", source=source(thread_id=" ")))["error"] == "invalid_argument"

    adapter.is_connected = False
    assert (await action.add_reaction("slack", "C1", "m", "eyes", source=source()))["error"] == "adapter_disconnected"
    adapter.is_connected = True
    runner._delivery_adapter_for = lambda src: (_ for _ in ()).throw(RuntimeError("route failure"))
    assert (await action.add_reaction("slack", "C1", "m", "eyes", source=source()))["error"] == "action_failed"
    setup_resolution(monkeypatch, adapter, profile=None)
    assert (await action.add_reaction("slack", "C1", "m", "eyes", source=source(profile=None)))["error"] == "adapter_not_registered"
    setup_resolution(monkeypatch, adapter, profile="")
    assert (await action.add_reaction("slack", "C1", "m", "eyes", source=source(profile=None)))["error"] == "adapter_not_registered"


@pytest.mark.asyncio
async def test_message_and_legacy_scope_mismatches_do_not_call_client(monkeypatch):
    adapter = Adapter()
    adapter._team_clients = {"T1": Client()}
    setup_resolution(monkeypatch, adapter)
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    for src in (source(message_id="other-ts"), source(guild_id="T2"),
                source(parent_chat_id="C2"), source(parent_chat_id=" "), source(parent_chat_id=3)):
        assert (await action.add_reaction("slack", "C1", "target-ts", "eyes", source=src))["error"] == "invalid_argument"
    adapter._team_clients["T1"].reactions_add.assert_not_awaited()


@pytest.mark.asyncio
async def test_primary_adapter_requires_exact_routed_destination(monkeypatch):
    adapter = Adapter()
    adapter._team_clients = {"T1": Client()}
    runner = setup_resolution(monkeypatch, adapter, profile="satellite")
    runner._owning_profile = lambda resolved, platform: (resolved is adapter, None)
    runner._routed_primary_adapter = lambda src: adapter if (
        src.scope_id == "T1" and src.chat_id == "C1" and src.user_id == "U1"
        and src.thread_id == "171.1") else None
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    assert (await action.add_reaction("slack", "C1", "m", "eyes", source=source(profile="satellite")))["ok"] is True
    for src in (source(profile="satellite", chat_id="C2"), source(profile="satellite", user_id="U2"),
                source(profile="satellite", thread_id="171.2"), source(profile="satellite", scope_id="T2")):
        result = await action.add_reaction("slack", "C1" if src["chat_id"] != "C2" else "C2", "m", "eyes", source=src)
        assert result["error"] == "adapter_not_registered"
    adapter._team_clients["T1"].reactions_add.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_reacted_is_idempotent_api_failure_is_structured(monkeypatch):
    adapter = Adapter()
    client = Client(SlackError({"error": "already_reacted"}))
    adapter._team_clients = {"T1": client}
    setup_resolution(monkeypatch, adapter)
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    result = await action.add_reaction("slack", "C1", "1", ":eyes:", source=source())
    assert result["ok"] is True
    client = Client(SlackError({"error": "ratelimited"}))
    adapter._team_clients["T1"] = client
    result = await action.add_reaction("slack", "C1", "1", ":eyes:", source=source())
    assert result == {"ok": False, "error": "action_failed", "detail": "Slack reactions_add failed"}


@pytest.mark.asyncio
async def test_existing_telegram_discord_signatures_remain_compatible(monkeypatch):
    action = pa.PlatformActions("plugin")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    adapter = types.SimpleNamespace(platform=types.SimpleNamespace(value="telegram"), is_connected=True,
                                    _set_reaction=AsyncMock(return_value=True))
    monkeypatch.setattr(action, "_resolve_adapter", lambda platform: (adapter, None))
    assert (await action.add_reaction("telegram", "c", "m", "x"))["ok"]
    assert await pa._discord_add_reaction(types.SimpleNamespace(_client=None), "c", "m", "x") == {
        "ok": False, "error": "adapter_disconnected", "detail": "discord client unavailable"}
