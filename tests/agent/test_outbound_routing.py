from __future__ import annotations

import os
import ssl
from types import SimpleNamespace

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


def test_agent_vault_plugin_derives_proxy_from_connection_environment(monkeypatch):
    monkeypatch.setenv("AGENT_VAULT_ADDR", "https://vault.example.test:18421")
    monkeypatch.setenv("AGENT_VAULT_TOKEN", "token/with spaces")
    monkeypatch.setenv("AGENT_VAULT_VAULT", "my/vault")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    from plugins.agent_vault import _resolve_profile_routing

    routing = _resolve_profile_routing()
    expected = "http://token%2Fwith%20spaces:my%2Fvault@vault.example.test:18422"
    assert routing["HTTP_PROXY"] == expected
    assert routing["HTTPS_PROXY"] == expected
    assert routing["OPENCLAW_PROXY_URL"] == expected
    assert routing["NODE_USE_ENV_PROXY"] == "1"


def test_agent_vault_plugin_keeps_existing_resolved_environment(monkeypatch):
    monkeypatch.setenv("AGENT_VAULT_ADDR", "http://vault.example.test:14321")
    monkeypatch.setenv("AGENT_VAULT_TOKEN", "token")
    monkeypatch.setenv("AGENT_VAULT_VAULT", "vault")
    monkeypatch.setenv("HTTPS_PROXY", "http://already-resolved:14322")

    from plugins.agent_vault import _resolve_profile_routing

    assert _resolve_profile_routing()["HTTPS_PROXY"] == "http://already-resolved:14322"


def test_agent_vault_plugin_does_not_resolve_partial_configuration(monkeypatch):
    monkeypatch.setenv("AGENT_VAULT_ADDR", "http://vault.example.test:14321")
    monkeypatch.delenv("AGENT_VAULT_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_VAULT_VAULT", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    from plugins.agent_vault import _resolve_profile_routing

    assert "HTTPS_PROXY" not in _resolve_profile_routing()


def test_slack_client_receives_agent_vault_ca_context(monkeypatch):
    ca_file = ssl.get_default_verify_paths().cafile
    if not ca_file or not os.path.isfile(ca_file):
        pytest.skip("system CA bundle is unavailable")
    monkeypatch.setenv("SSL_CERT_FILE", ca_file)

    from plugins.platforms.slack.adapter import _apply_slack_proxy

    client = SimpleNamespace(proxy=None, ssl=None)
    _apply_slack_proxy(client, "http://127.0.0.1:14322")

    assert client.proxy == "http://127.0.0.1:14322"
    assert client.ssl is not None
    assert client.ssl.verify_mode == ssl.CERT_REQUIRED


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
