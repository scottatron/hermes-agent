"""Profile-aware Agent Vault outbound routing plugin."""
from __future__ import annotations

import os
from collections.abc import Mapping

from agent.secret_scope import current_secret_scope


_ROUTING_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "NODE_USE_ENV_PROXY", "OPENCLAW_PROXY_URL",
    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "DENO_CERT",
)


def _resolve_profile_routing() -> Mapping[str, str]:
    scope = current_secret_scope()
    source = scope if scope is not None else os.environ
    return {key: source[key] for key in _ROUTING_KEYS if source.get(key)}


def register(ctx) -> None:
    ctx.register_outbound_routing_provider(_resolve_profile_routing)
