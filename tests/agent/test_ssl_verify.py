"""Tests for agent.ssl_verify.resolve_httpx_verify."""

import ssl
from pathlib import Path
from unittest.mock import patch

import certifi
import pytest

from agent.outbound_routing import (
    clear_outbound_routing_provider,
    register_outbound_routing_provider,
)
from agent.ssl_verify import resolve_httpx_verify

_CA_ENV_VARS = ("HERMES_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


@pytest.fixture
def clean_ca_env(monkeypatch):
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    clear_outbound_routing_provider()
    yield
    clear_outbound_routing_provider()




def test_hermes_ca_bundle_returns_ssl_context(clean_ca_env, monkeypatch):
    monkeypatch.setenv("HERMES_CA_BUNDLE", certifi.where())
    result = resolve_httpx_verify()
    assert isinstance(result, ssl.SSLContext)






def test_default_without_env_is_true(clean_ca_env):
    assert resolve_httpx_verify() is True


def test_outbound_routing_ca_bundle_returns_ssl_context(clean_ca_env):
    register_outbound_routing_provider(
        lambda: {"SSL_CERT_FILE": certifi.where()}
    )

    result = resolve_httpx_verify()

    assert isinstance(result, ssl.SSLContext)


def test_outbound_routing_ca_overrides_stale_process_ca(
    clean_ca_env, monkeypatch, tmp_path
):
    process_ca = tmp_path / "process-ca.pem"
    routing_ca = tmp_path / "routing-ca.pem"
    ca_contents = Path(certifi.where()).read_text(encoding="utf-8")
    process_ca.write_text(ca_contents)
    routing_ca.write_text(ca_contents)
    monkeypatch.setenv("SSL_CERT_FILE", str(process_ca))
    register_outbound_routing_provider(
        lambda: {
            "HTTPS_PROXY": "http://profile-proxy:14322",
            "SSL_CERT_FILE": str(routing_ca),
        }
    )

    with patch(
        "agent.ssl_verify.ssl.create_default_context",
        wraps=ssl.create_default_context,
    ) as create_context:
        result = resolve_httpx_verify()

    assert isinstance(result, ssl.SSLContext)
    create_context.assert_called_once_with(cafile=str(routing_ca))
