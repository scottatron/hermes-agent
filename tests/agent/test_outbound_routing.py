from __future__ import annotations

import os

import pytest

from agent.outbound_routing import (
    clear_outbound_routing_provider,
    get_outbound_routing_env,
    outbound_routing_cache_key,
    register_outbound_routing_provider,
)
from agent.secret_scope import reset_secret_scope, set_secret_scope


@pytest.fixture(autouse=True)
def _clear_provider():
    clear_outbound_routing_provider()
    yield
    clear_outbound_routing_provider()


def test_provider_is_context_local_and_filters_non_routing_values():
    def resolver():
        from agent.secret_scope import current_secret_scope

        return current_secret_scope() or {}

    register_outbound_routing_provider(resolver)
    token = set_secret_scope({
        "HTTPS_PROXY": "http://squirl-proxy:14322",
        "SLACK_PROXY": "http://squirl-slack-proxy:14322",
        "SSL_CERT_FILE": "/tmp/squirl-ca.pem",
        "AGENT_VAULT_TOKEN": "must-not-leak",
    })
    try:
        assert get_outbound_routing_env() == {
            "HTTPS_PROXY": "http://squirl-proxy:14322",
            "SLACK_PROXY": "http://squirl-slack-proxy:14322",
            "SSL_CERT_FILE": "/tmp/squirl-ca.pem",
        }
    finally:
        reset_secret_scope(token)


def test_cache_key_separates_profile_proxy_identities():
    state = {"HTTPS_PROXY": "http://squirl-proxy:14322"}
    register_outbound_routing_provider(lambda: state)
    squirl_key = outbound_routing_cache_key()
    state["HTTPS_PROXY"] = "http://moneytron-proxy:14322"
    assert outbound_routing_cache_key() != squirl_key


def test_auxiliary_client_cache_separates_profile_proxy_identities():
    from agent.auxiliary_client import _client_cache_key

    state = {"HTTPS_PROXY": "http://squirl-proxy:14322"}
    register_outbound_routing_provider(lambda: state)
    squirl_key = _client_cache_key("openai", async_mode=False, model="gpt-test")
    state["HTTPS_PROXY"] = "http://moneytron-proxy:14322"
    moneytron_key = _client_cache_key("openai", async_mode=False, model="gpt-test")

    assert moneytron_key != squirl_key


def test_agent_vault_plugin_reads_active_secret_scope(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://global-proxy:14322")
    from plugins.agent_vault import _resolve_profile_routing

    token = set_secret_scope({"HTTPS_PROXY": "http://profile-proxy:14322"})
    try:
        assert _resolve_profile_routing()["HTTPS_PROXY"] == "http://profile-proxy:14322"
    finally:
        reset_secret_scope(token)


def test_mcp_and_tool_children_receive_profile_routing():
    register_outbound_routing_provider(
        lambda: {
            "HTTPS_PROXY": "http://profile-proxy:14322",
            "REQUESTS_CA_BUNDLE": "/tmp/profile-ca.pem",
        }
    )
    from tools.code_execution_tool import _scrub_child_env
    from tools.environments.local import (
        _make_run_env,
        _sanitize_subprocess_env,
        hermes_subprocess_env,
    )
    from tools.mcp_tool import _build_safe_env

    for env in (
        _build_safe_env(None),
        _make_run_env({"PATH": os.environ.get("PATH", "")}),
        _sanitize_subprocess_env({"PATH": os.environ.get("PATH", "")}),
        hermes_subprocess_env(),
        _scrub_child_env({}, is_passthrough=lambda _: False, is_windows=False),
    ):
        assert env["HTTPS_PROXY"] == "http://profile-proxy:14322"
        assert env["REQUESTS_CA_BUNDLE"] == "/tmp/profile-ca.pem"
