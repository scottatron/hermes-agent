"""Slack reaction observations remain routed, authorized, and independent of agent triggers."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner
from plugins.platforms.slack.adapter import SlackAdapter


def adapter():
    value = SlackAdapter(PlatformConfig(enabled=True, token="test"))
    value._bot_user_id = "Ubot"
    value._channel_team = {"Ctest": "Ttest"}
    value._team_clients = {}
    value._slack_reaction_triggers = lambda: None
    return value


def event(**changes):
    value = dict(item={"type": "message", "channel": "Ctest", "ts": "123.000"},
                 user="Uscott", reaction="eyes", event_ts="124.000")
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_added_and_removed_publish_plain_observations_without_turn_or_history():
    value = adapter()
    value._platform_event_handler = AsyncMock()
    value._handle_slack_message = AsyncMock()
    value._reaction_thread_ts = AsyncMock()
    for removed in (False, True):
        await value._handle_slack_reaction(event(), removed=removed)
    calls = value._platform_event_handler.await_args_list
    assert [call.args[0]["payload"]["action"] for call in calls] == ["added", "removed"]
    envelope, source = calls[0].args
    assert envelope == {"platform": "slack", "event_type": "reaction", "payload": {
        "chat_id": "Ctest", "message_id": "123.000", "thread_id": None,
        "user_id": "Uscott", "team_id": "Ttest", "event_ts": "124.000",
        "reaction": "eyes", "action": "added",
    }}
    assert source.user_id == "Uscott" and source.scope_id == "Ttest"
    assert source._transport_adapter_ref() is value
    value._handle_slack_message.assert_not_awaited()
    value._reaction_thread_ts.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_transport_routes_to_owner_and_auth_denial_does_not_publish(tmp_path):
    value = adapter()
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True, profile_routes=[
        ProfileRoute(name="capture", platform="slack", profile="work", chat_id="Ctest")])
    value.gateway_runner = runner
    work = tmp_path / "profiles" / "work"
    work.mkdir(parents=True)
    runner._resolve_profile_home_for_source = lambda source: work
    scopes = []
    runner._is_user_authorized_for_source = lambda source: scopes.append(source.profile) or False
    value._platform_event_handler = runner._make_default_profile_platform_event_handler()
    with (patch("gateway.run._multiplex_profile_homes", return_value=[("work", work)]),
          patch("gateway.run._profile_runtime_scope", side_effect=lambda home: nullcontext()),
          patch("hermes_cli.lifecycle.has_hook", return_value=True),
          patch("hermes_cli.lifecycle.invoke_hook") as invoke):
        await value._handle_slack_reaction(event())
        assert scopes == ["work"]
        invoke.assert_not_called()
        runner._is_user_authorized_for_source = lambda source: True
        await value._handle_slack_reaction(event())
        assert invoke.call_args.kwargs["payload"]["message_id"] == "123.000"


@pytest.mark.asyncio
async def test_malformed_or_ambiguous_events_drop_and_observer_failure_preserves_legacy_hooks():
    value = adapter()
    value._platform_event_handler = AsyncMock()
    for changes in [dict(event_ts=None), dict(user="Ubot"), dict(reaction=""),
                    dict(item={"type": "file", "channel": "Ctest", "ts": "123.000"})]:
        await value._handle_slack_reaction(event(**changes))
    value._platform_event_handler.assert_not_awaited()
    value._channel_team = {}
    value._team_clients = {"Tone": None, "Ttwo": None}
    await value._handle_slack_reaction(event())
    value._platform_event_handler.assert_not_awaited()
    value._channel_team = {"Ctest": "Ttest"}
    value._platform_event_handler.side_effect = RuntimeError("observer failed")
    value._reaction_handler = AsyncMock()
    await value._handle_slack_reaction(event())
    value._reaction_handler.assert_awaited_once()
