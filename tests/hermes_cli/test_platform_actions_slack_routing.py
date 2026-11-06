"""New facade against real session identity and shared-bot route resolution."""

import asyncio
from dataclasses import replace
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
from gateway.profile_routing import ProfileRoute
from hermes_cli.platform_actions import PlatformActions


@pytest.fixture
def routing(monkeypatch):
    clients = {team: SimpleNamespace(reactions_add=AsyncMock()) for team in ("T1", "T2")}
    primary = SimpleNamespace(platform=Platform.SLACK, is_connected=True, _team_clients=clients)
    runner = GatewayAuthorizationMixin.__new__(GatewayAuthorizationMixin)
    runner.adapters = {Platform.SLACK: primary}
    runner._profile_adapters = {"baymax": {}}
    runner._profile_bot_platforms = {}
    runner._primary_profile_name = "default"
    runner._active_profile_name = lambda: "default"
    runner.config = SimpleNamespace(
        multiplex_profiles=True, platforms={}, profile_routes=[ProfileRoute(
            name="review-channel", platform="slack", profile="baymax",
            guild_id="T1", chat_id="C1", user_id="U1")])
    module = ModuleType("gateway.run")
    module._gateway_runner_ref = lambda: runner
    module._multiplex_profile_homes = lambda config: [
        ("baymax", Path("/tmp/contract/profiles/baymax"))]
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "baymax")
    action = PlatformActions("baymax")
    monkeypatch.setattr(action, "_capability_granted", lambda: True)
    return runner, primary, clients, action


def snapshot(**updates):
    source = dict(platform="slack", profile="baymax", chat_id="C1", scope_id="T1",
                  user_id="U1", thread_id="100.001", message_id="100.002")
    source.update(updates)
    return source


@pytest.mark.parametrize("thread", [None, "100.001"])
def test_real_shared_route_preserves_trigger_and_workspace(routing, thread):
    _, _, clients, action = routing

    result = asyncio.run(action.add_reaction(
        "slack", "C1", "100.002", "eyes", source=snapshot(thread_id=thread)))

    assert result["ok"] is True
    clients["T1"].reactions_add.assert_awaited_once_with(
        channel="C1", timestamp="100.002", name="eyes")
    clients["T2"].reactions_add.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"chat_id": "C-other"},
    {"chat_id": "C-other", "parent_chat_id": "C1"},
    {"user_id": "U-other"}, {"scope_id": "T2"},
    {"parent_chat_id": " "}, {"parent_chat_id": object()},
])
def test_real_shared_route_rejects_wrong_destination_or_sender(routing, changes):
    _, _, clients, action = routing
    source = snapshot(**changes)

    result = asyncio.run(action.add_reaction(
        "slack", source["chat_id"], "100.002", "eyes", source=source))

    assert result["ok"] is False
    for client in clients.values():
        client.reactions_add.assert_not_awaited()


def test_real_thread_route_must_match(routing):
    runner, _, clients, action = routing
    runner.config.profile_routes[0] = replace(
        runner.config.profile_routes[0], thread_id="allowed-root")

    denied = asyncio.run(action.add_reaction(
        "slack", "C1", "100.002", "eyes", source=snapshot()))
    assert denied["ok"] is False
    allowed = asyncio.run(action.add_reaction(
        "slack", "C1", "100.002", "eyes", source=snapshot(thread_id="allowed-root")))

    assert allowed["ok"] is True
    assert clients["T1"].reactions_add.await_count == 1


def test_disconnected_owned_bot_never_borrows_primary(routing):
    runner, _, clients, action = routing
    runner._profile_bot_platforms["baymax"] = {Platform.SLACK}

    result = asyncio.run(action.add_reaction(
        "slack", "C1", "100.002", "eyes", source=snapshot()))

    assert result["ok"] is False
    clients["T1"].reactions_add.assert_not_awaited()


def test_owned_secondary_uses_its_own_workspace_client(routing):
    runner, _, clients, action = routing
    own = SimpleNamespace(reactions_add=AsyncMock())
    runner._profile_adapters["baymax"][Platform.SLACK] = SimpleNamespace(
        platform=Platform.SLACK, is_connected=True, _team_clients={"T1": own})
    runner._profile_bot_platforms["baymax"] = {Platform.SLACK}

    result = asyncio.run(action.add_reaction(
        "slack", "C1", "100.002", "eyes", source=snapshot()))

    assert result["ok"] is True
    own.reactions_add.assert_awaited_once()
    clients["T1"].reactions_add.assert_not_awaited()
