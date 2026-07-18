"""Context-aware outbound routing supplied by optional plugins.

The registry deliberately exposes only proxy and trust configuration. Provider
credentials remain governed by :mod:`agent.secret_scope` and must never be
copied into arbitrary tool subprocesses.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import Optional

logger = logging.getLogger(__name__)

OutboundRoutingProvider = Callable[[], Mapping[str, str]]

_ROUTING_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "NODE_USE_ENV_PROXY", "OPENCLAW_PROXY_URL",
    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "DENO_CERT",
})
_provider: Optional[OutboundRoutingProvider] = None


def register_outbound_routing_provider(provider: OutboundRoutingProvider) -> None:
    """Register the process-wide resolver used in the active profile context."""
    global _provider
    if _provider is not None and _provider is not provider:
        raise RuntimeError("an outbound routing provider is already registered")
    _provider = provider


def clear_outbound_routing_provider() -> None:
    """Reset the provider (primarily for tests and plugin reloads)."""
    global _provider
    _provider = None


def get_outbound_routing_env() -> dict[str, str]:
    """Resolve safe routing variables for the current profile context."""
    if _provider is None:
        return {}
    try:
        values = _provider() or {}
    except Exception:
        logger.warning("outbound routing provider failed", exc_info=True)
        return {}
    return {
        key: str(value)
        for key, value in values.items()
        if key in _ROUTING_KEYS and value is not None and str(value).strip()
    }


def apply_outbound_routing_env(env: dict[str, str]) -> dict[str, str]:
    """Overlay the current profile's routing variables onto ``env`` in place."""
    env.update(get_outbound_routing_env())
    return env


def outbound_routing_cache_key() -> str:
    """Return a non-secret identity suitable for shared HTTP-client caches."""
    values = get_outbound_routing_env()
    material = "\0".join(f"{key}={values[key]}" for key in sorted(values))
    return hashlib.sha256(material.encode("utf-8")).hexdigest() if material else ""
