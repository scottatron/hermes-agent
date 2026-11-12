"""Bolt acknowledgements must not become an egress path around Slack output policy."""

import pytest

from gateway import output_policy
from gateway.config import PlatformConfig
from gateway.output_policy import OutputPolicy, bind_output_policy, reset_output_policy
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.mark.asyncio
async def test_protected_shared_profile_gets_empty_slash_ack(monkeypatch):
    monkeypatch.setattr(output_policy, "_protected_profiles", {"/profile/protected"})
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    dispatched = []
    adapter._handle_slash_command = lambda command: _record(dispatched, command)
    acknowledgements = []

    async def ack(**kwargs):
        acknowledgements.append(kwargs)

    await adapter._handle_hermes_command(ack, {
        "command": "/status", "channel_id": "D123", "team_id": "T123", "user_id": "U123",
    })

    assert acknowledgements == [{}]
    assert dispatched


@pytest.mark.asyncio
async def test_unprotected_profile_keeps_running_slash_ack():
    token = bind_output_policy(OutputPolicy("/profile/normal", "allow"))
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._handle_slash_command = lambda _command: _done()
    acknowledgements = []

    async def ack(**kwargs):
        acknowledgements.append(kwargs)

    try:
        await adapter._handle_hermes_command(ack, {
            "command": "/status", "channel_id": "D123", "team_id": "T123", "user_id": "U123",
        })
    finally:
        reset_output_policy(token)

    assert acknowledgements[0]["response_type"] == "ephemeral"
    assert acknowledgements[0]["text"]


async def _record(items, value):
    items.append(value)


async def _done():
    return None
