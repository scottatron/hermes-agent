"""Profile-aware Agent Vault outbound routing plugin."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

from agent.secret_scope import current_secret_scope


_AGENT_VAULT_ADDR = "AGENT_VAULT_ADDR"
_AGENT_VAULT_TOKEN = "AGENT_VAULT_TOKEN"
_AGENT_VAULT_VAULT = "AGENT_VAULT_VAULT"
_DEFAULT_CONTROL_PORT = 14321
_CA_ENV_KEYS = (
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "DENO_CERT",
)


def _agent_vault_proxy_env(source: Mapping[str, str]) -> dict[str, str]:
    """Derive Agent Vault's routing environment from its connection tuple.

    ``agent-vault run`` normally supplies these values before Hermes starts.
    Hermes can also be started directly, though, so derive the same proxy
    settings when only the broker address, token, and vault are available.
    """
    address = str(source.get(_AGENT_VAULT_ADDR, "")).strip()
    token = str(source.get(_AGENT_VAULT_TOKEN, "")).strip()
    vault = str(source.get(_AGENT_VAULT_VAULT, "")).strip()
    if not address or not token or not vault:
        return dict(source)

    parsed = urlsplit(address)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return dict(source)

    try:
        control_port = parsed.port
    except ValueError:
        return dict(source)

    proxy_port = (control_port or _DEFAULT_CONTROL_PORT) + 1
    if not 1 <= proxy_port <= 65535:
        return dict(source)

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    proxy = f"http://{quote(token, safe='')}:{quote(vault, safe='')}@{host}:{proxy_port}"
    resolved = dict(source)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "OPENCLAW_PROXY_URL"):
        resolved[key] = proxy
    resolved["NODE_USE_ENV_PROXY"] = "1"

    ca_path = Path.home() / ".agent-vault" / "mitm-ca.pem"
    if ca_path.is_file():
        for key in _CA_ENV_KEYS:
            resolved[key] = str(ca_path)
    return resolved


def _resolve_profile_routing() -> Mapping[str, str]:
    scope = current_secret_scope()
    source = scope if scope is not None else os.environ
    # The core outbound-routing registry owns the allowlist. Returning the
    # active scope here keeps new generic routing keys out of this vendor
    # plugin and prevents the two sides from drifting.
    # When the connection tuple is available, it is authoritative. A parent
    # process may already have injected a proxy and a different CA bundle;
    # preserving those values would route through Agent Vault while verifying
    # its MITM certificate with the wrong trust root.
    has_connection = all(
        str(source.get(key, "")).strip()
        for key in (_AGENT_VAULT_ADDR, _AGENT_VAULT_TOKEN, _AGENT_VAULT_VAULT)
    )
    resolved = _agent_vault_proxy_env(source) if has_connection else source
    if not any(resolved.get(key) for key in _CA_ENV_KEYS):
        ca_path = Path.home() / ".agent-vault" / "mitm-ca.pem"
        if ca_path.is_file():
            resolved = dict(resolved)
            for key in _CA_ENV_KEYS:
                resolved[key] = str(ca_path)
    return resolved


def register(ctx) -> None:
    ctx.register_outbound_routing_provider(_resolve_profile_routing)
