"""Behavioral contract for profile-scoped Slack transport silence."""

import pytest

from gateway import output_policy
from gateway.output_policy import (
    OutputPolicy,
    bind_output_policy,
    current_output_policy,
    profile_output_policy,
    reset_output_policy,
)
from gateway.config import PlatformConfig
from gateway.run_output_policy import _activate_profile_policy, profile_output_policy_scope
from plugins.platforms.slack import adapter as slack_adapter_module
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.adapter_output_policy import require_slack_output_allowed
from plugins.platforms.slack.adapter_output_policy import (
    SlackOutputDenied,
    install_slack_api_guard,
)


class FakeWebClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def api_call(self, api_method, **kwargs):
        self.calls.append(api_method)
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        return await self.api_call("chat.postMessage", **kwargs)

    async def users_info(self, **kwargs):
        return await self.api_call("users.info", **kwargs)

    async def auth_test(self, **kwargs):
        return await self.api_call("auth.test", **kwargs)

    async def reactions_add(self, **kwargs):
        return await self.api_call("reactions.add", **kwargs)

    async def assistant_threads_setStatus(self, **kwargs):
        return await self.api_call("assistant.threads.setStatus", **kwargs)

    async def files_upload_v2(self, **kwargs):
        return await self.api_call("files.getUploadURLExternal", **kwargs)


@pytest.mark.asyncio
async def test_denied_profile_blocks_messages_reactions_and_file_uploads_before_api_call():
    client = install_slack_api_guard(FakeWebClient(), OutputPolicy("/profile/a", "deny"))

    with pytest.raises(SlackOutputDenied):
        await client.chat_postMessage(channel="C1", text="hello")
    with pytest.raises(SlackOutputDenied):
        await client.api_call("reactions.add", json={"channel": "C1"})
    with pytest.raises(SlackOutputDenied):
        await client.reactions_add(channel="C1", name="eyes", timestamp="1.2")
    with pytest.raises(SlackOutputDenied):
        await client.assistant_threads_setStatus(channel_id="C1", thread_ts="1.2", status="Thinking")
    with pytest.raises(SlackOutputDenied):
        await client.files_upload_v2(channel="C1", file=b"data")

    assert client.calls == []


@pytest.mark.asyncio
async def test_bound_allow_cannot_override_a_protected_transport_owner():
    client = install_slack_api_guard(FakeWebClient(), OutputPolicy("/transport-owner", "deny"))
    token = bind_output_policy(OutputPolicy("/routed-profile", "allow"))
    try:
        with pytest.raises(SlackOutputDenied):
            await client.chat_postMessage(channel="C1", text="blocked by protected transport")
    finally:
        reset_output_policy(token)

    assert client.calls == []


@pytest.mark.asyncio
async def test_guard_installation_is_idempotent_and_later_deny_is_not_weakened():
    client = FakeWebClient()
    install_slack_api_guard(client, OutputPolicy("/profile/a", "allow"))
    install_slack_api_guard(client, OutputPolicy("/profile/a", "allow"))

    await client.users_info(user="U1")
    install_slack_api_guard(client, OutputPolicy("/profile/a", "deny"))
    install_slack_api_guard(client, OutputPolicy("/profile/a", "allow"))
    with pytest.raises(SlackOutputDenied):
        await client.chat_postMessage(channel="C1", text="must remain blocked")

    assert client.calls == ["users.info"]


@pytest.mark.asyncio
async def test_new_same_profile_latch_tightens_in_flight_allow_snapshot(monkeypatch):
    protected = set()
    monkeypatch.setattr(output_policy, "_protected_profiles", protected)
    client = install_slack_api_guard(FakeWebClient(), OutputPolicy("/profile/a", "allow"))
    token = bind_output_policy(OutputPolicy("/profile/a", "allow"))
    try:
        output_policy.register_protected_profile("/profile/a")
        with pytest.raises(SlackOutputDenied):
            await client.chat_postMessage(channel="C1", text="deny latch wins")
    finally:
        reset_output_policy(token)
    assert client.calls == []


@pytest.mark.asyncio
async def test_protected_roster_does_not_block_differently_bound_allow_profile(monkeypatch):
    monkeypatch.setattr(output_policy, "_protected_profiles", {"/profile/protected"})
    client = install_slack_api_guard(FakeWebClient())
    token = bind_output_policy(OutputPolicy("/profile/allowed", "allow"))
    try:
        await client.chat_postMessage(channel="C1", text="allowed profile")
    finally:
        reset_output_policy(token)
    assert client.calls == ["chat.postMessage"]


@pytest.mark.asyncio
async def test_unbound_protected_client_allows_read_api_but_denies_unknown_methods():
    client = install_slack_api_guard(FakeWebClient(), OutputPolicy("/profile/a", "deny"))

    await client.auth_test()
    await client.users_info(user="U1")
    await client.api_call("apps.connections.open")
    with pytest.raises(SlackOutputDenied):
        await client.api_call("future.writeMethod")

    assert client.calls == ["auth.test", "users.info", "apps.connections.open"]


@pytest.mark.asyncio
async def test_explicit_deny_context_blocks_even_when_transport_owner_is_unprotected():
    client = install_slack_api_guard(FakeWebClient())
    token = bind_output_policy(OutputPolicy("/routed-profile", "deny"))
    try:
        with pytest.raises(SlackOutputDenied):
            await client.chat_postMessage(channel="C1", text="blocked")
    finally:
        reset_output_policy(token)

    assert client.calls == []


@pytest.mark.asyncio
async def test_slack_adapter_factory_installs_policy_guard(monkeypatch):
    monkeypatch.setattr(slack_adapter_module, "AsyncWebClient", FakeWebClient)
    token = bind_output_policy(OutputPolicy("/profile/a", "deny"))
    try:
        adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
        client = adapter._new_web_client("xoxb-test", None)
    finally:
        reset_output_policy(token)

    with pytest.raises(SlackOutputDenied):
        await client.chat_postMessage(channel="C1", text="blocked")
    assert client.calls == []


@pytest.mark.asyncio
async def test_bolt_per_request_client_gets_the_same_transport_guard():
    client = FakeWebClient()
    middleware = slack_adapter_module._slack_per_request_proxy_middleware(
        None, OutputPolicy("/profile/a", "deny"))

    async def next_():
        await client.chat_postMessage(channel="C1", text="blocked")

    with pytest.raises(SlackOutputDenied):
        await middleware(client, next_)
    assert client.calls == []


@pytest.mark.asyncio
async def test_slash_response_url_path_refuses_before_opening_http_session(monkeypatch):
    class UnexpectedHttp:
        def __init__(self, **kwargs):
            raise AssertionError("protected response_url must not open an HTTP session")

    monkeypatch.setattr(slack_adapter_module, "aiohttp", type("Http", (), {"ClientSession": UnexpectedHttp}))
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    token = bind_output_policy(OutputPolicy("/profile/a", "deny"))
    try:
        result = await adapter._send_slash_ephemeral(
            {"response_url": "https://hooks.slack.test/response"}, "private reply")
    finally:
        reset_output_policy(token)

    assert not result.success


def test_unbound_response_url_write_reads_profile_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(output_policy, "_protected_profiles", set())
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *_args, **_kwargs: {
        "security": {"output_policy": {"slack": "deny"}},
    })

    with pytest.raises(SlackOutputDenied):
        require_slack_output_allowed("response_url")


def test_profile_scope_binds_deny_and_persists_it_before_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(output_policy, "_protected_profiles", set())
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *_args, **_kwargs: {
        "security": {"output_policy": {"slack": "deny"}},
    })

    with profile_output_policy_scope(tmp_path):
        active = current_output_policy()
        assert isinstance(active, OutputPolicy)
        assert active.slack == "deny"

    marker = tmp_path / "security" / "output_policy.json"
    assert marker.exists()
    monkeypatch.setattr(output_policy, "_protected_profiles", set())
    _activate_profile_policy(tmp_path, {"security": {"output_policy": {"slack": "allow"}}})
    assert profile_output_policy(tmp_path, {"security": {"output_policy": {"slack": "allow"}}}).slack == "deny"


def test_boot_roster_latches_protected_profiles_before_shared_transport_output(tmp_path, monkeypatch):
    monkeypatch.setattr(output_policy, "_protected_profiles", set())
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *_args, **_kwargs: {
        "security": {"output_policy": {"slack": "deny"}},
    })

    from gateway.run_output_policy import register_output_policy_roster

    register_output_policy_roster([("protected", tmp_path)])

    assert str(tmp_path) in output_policy._protected_profiles
    assert output_policy.slack_output_denied()


def test_malformed_policy_section_denies_output(tmp_path):
    policy = profile_output_policy(tmp_path, {"security": {"output_policy": []}})

    assert policy == OutputPolicy(str(tmp_path), "deny")
