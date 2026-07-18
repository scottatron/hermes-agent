"""Profile-aware Agent Vault outbound routing plugin."""
from __future__ import annotations

import os
from collections.abc import Mapping

from agent.secret_scope import current_secret_scope


def _resolve_profile_routing() -> Mapping[str, str]:
    scope = current_secret_scope()
    source = scope if scope is not None else os.environ
    # The core outbound-routing registry owns the allowlist. Returning the
    # active scope here keeps new generic routing keys out of this vendor
    # plugin and prevents the two sides from drifting.
    return source


def register(ctx) -> None:
    ctx.register_outbound_routing_provider(_resolve_profile_routing)
