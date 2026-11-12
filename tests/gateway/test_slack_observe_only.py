"""Protected Slack input may be offered to intake, then must not reach normal dispatch."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import output_policy
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.output_policy import OutputPolicy, bind_output_policy, reset_output_policy
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _slack_event() -> MessageEvent:
    return MessageEvent(
        text="ordinary text",
        message_id="m1",
        source=SessionSource(
            platform=Platform.SLACK,
            user_id="U123",
            chat_id="D123",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _runner(home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True)})
    runner._resolve_profile_home_for_source = lambda _source: home
    runner._intake_adapter_for = lambda _source: None
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._admit_bot_message_for_source = MagicMock(return_value=True)
    runner._admit_bot_chain = MagicMock(return_value=True)
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._handle_message_with_agent = AsyncMock()
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    [],
    [{"action": "skip"}],
    [{"action": "allow"}],
    [{"unknown": "malformed"}],
    RuntimeError("plugin failed"),
])
async def test_protected_ingress_drops_after_missing_failed_or_nonconsuming_hook(monkeypatch, tmp_path, result):
    monkeypatch.setattr(output_policy, "_protected_profiles", {str(tmp_path)})
    monkeypatch.setattr("gateway.run._is_slack_ignored_channel", lambda *_args: False)
    called = []

    async def invoke(name, **_kwargs):
        assert name == "pre_gateway_dispatch"
        called.append(name)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", invoke)
    runner = _runner(tmp_path)
    token = bind_output_policy(OutputPolicy(str(tmp_path), "allow"))
    try:
        outcome = await runner._hm_admit_event(_slack_event())
    finally:
        reset_output_policy(token)

    assert outcome is None
    assert called == ["pre_gateway_dispatch"]
    runner._is_user_authorized_for_source.assert_not_called()
    runner._admit_bot_message_for_source.assert_not_called()
    assert runner.session_store.mock_calls == []
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_protected_busy_slack_input_runs_intake_then_never_queues_or_dispatches(monkeypatch, tmp_path):
    from gateway.run import _async_profile_runtime_scope
    from plugins.platforms.slack.adapter import SlackAdapter

    @asynccontextmanager
    async def no_scope(_home):
        yield

    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", no_scope)
    monkeypatch.setattr(output_policy, "_protected_profiles", {str(tmp_path)})
    runner = _runner(tmp_path)
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(return_value=None)
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter.gateway_runner = runner
    adapter._busy_session_handler = AsyncMock(return_value=True)
    adapter._shared_runtime_owns_input = MagicMock(return_value=True)
    event = _slack_event()
    token = bind_output_policy(OutputPolicy(str(tmp_path), "allow"))
    try:
        await adapter._handle_message_while_active(event, "session-1")
    finally:
        reset_output_policy(token)

    runner._hm_pre_gateway_dispatch_hook.assert_awaited_once()
    adapter._busy_session_handler.assert_not_awaited()
    adapter._shared_runtime_owns_input.assert_not_called()
    assert event._gateway_accepted is True


@pytest.mark.asyncio
async def test_unprotected_shared_busy_input_keeps_fifo_dispatch(monkeypatch, tmp_path):
    from plugins.platforms.slack.adapter import SlackAdapter

    dispatched = AsyncMock()
    monkeypatch.setattr("gateway.session_ingress_burst.dispatch_busy_burst", dispatched)
    monkeypatch.setattr(output_policy, "_protected_profiles", set())
    runner = _runner(tmp_path)
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter.gateway_runner = runner
    adapter._shared_runtime_owns_input = MagicMock(return_value=True)
    event = _slack_event()
    token = bind_output_policy(OutputPolicy(str(tmp_path), "allow"))
    try:
        await adapter._handle_message_while_active(event, "session-1")
    finally:
        reset_output_policy(token)

    dispatched.assert_awaited_once_with(adapter, event, "session-1")
