"""Host-to-sandbox environment forwarding, shared by exec-style backends.

``DockerEnvironment`` grew this logic first, as private module functions in
``tools/environments/docker.py``. This module is a generic restatement of it
for backends that cannot use ``docker exec -e`` and must inject variables as
shell text instead.

Why the duplication is deliberate: ``docker.py`` is upstream's file and moves
constantly, while the Kubernetes backend is a fork-local carried patch that
must stay independently droppable (see ``CARRYING_PATCHES.md``). Rewiring
``docker.py`` through here would make that patch conflict on every upstream
change to a 2000-line file. A new module conflicts with nothing.

The one thing that must not drift is *which* variables are eligible for
forwarding, because that is a security boundary — so the blocklist itself is
imported from its authoritative home in ``tools/environments/local.py`` rather
than restated, and ``tests/tools/test_env_forwarding.py`` pins this module's
resolution against Docker's.
"""

import logging
import os
import re
import shlex

logger = logging.getLogger(__name__)

ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_forward_env_names(forward_env, *, config_key: str) -> list[str]:
    """Return a deduplicated list of valid environment variable names.

    *config_key* names the config.yaml setting being validated so warnings
    point at the key the operator actually wrote.
    """
    if forward_env and not isinstance(forward_env, (list, tuple)):
        # A bare string is the likely config.yaml slip, and it is the dangerous
        # one: iterating it yields single characters, every one of which is a
        # *valid* env var name, so `k8s_forward_env: "GH_TOKEN"` would quietly
        # forward $G, $H, $_, $T… instead of being rejected.
        logger.warning("%s is not a list: %r", config_key, forward_env)
        return []

    normalized: list[str] = []
    seen: set[str] = set()

    for item in forward_env or []:
        if not isinstance(item, str):
            logger.warning("Ignoring non-string %s entry: %r", config_key, item)
            continue

        key = item.strip()
        if not key:
            continue
        if not ENV_VAR_NAME_RE.match(key):
            logger.warning("Ignoring invalid %s entry: %r", config_key, item)
            continue
        if key in seen:
            continue

        seen.add(key)
        normalized.append(key)

    return normalized


def normalize_env_dict(env, *, config_key: str) -> dict[str, str]:
    """Validate and normalize an env mapping to ``{str: str}``.

    Entries with invalid variable names or non-scalar values are dropped.
    """
    if not env:
        return {}
    if not isinstance(env, dict):
        logger.warning("%s is not a dict: %r", config_key, env)
        return {}

    normalized: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_VAR_NAME_RE.match(key.strip()):
            logger.warning("Ignoring invalid %s key: %r", config_key, key)
            continue
        key = key.strip()
        if not isinstance(value, str):
            # Coerce simple scalars (int, bool, float) — YAML produces these
            # for unquoted values — and reject anything structured.
            if isinstance(value, (int, float, bool)):
                value = str(value)
            else:
                logger.warning(
                    "Ignoring non-string %s value for %r: %r", config_key, key, value
                )
                continue
        normalized[key] = value

    return normalized


def load_hermes_env_vars() -> dict[str, str]:
    """Load ``~/.hermes/.env`` values without failing command execution."""
    try:
        from hermes_cli.config import load_env

        return load_env() or {}
    except Exception:
        return {}


def resolve_passthrough_env(forward_env) -> tuple[dict[str, str], set[str]]:
    """Return forwarded values, plus scoped names that must be unset.

    Explicit ``forward_env`` entries are an intentional operator opt-in and
    win over the generic Hermes secret blocklist; only *implicit* passthrough
    keys (registered by skills) are filtered through it. The second return
    value carries names that exist as passthrough but resolve to nothing under
    the active profile scope — those must be actively unset in the sandbox, or
    a previous profile's value would still be visible through the shared
    session snapshot.
    """
    exec_env: dict[str, str] = {}
    explicit_forward_keys = set(forward_env or ())
    passthrough_keys: set[str] = set()
    resolve_value = None
    multiplex_active = False
    is_global_env = lambda _name: False  # noqa: E731
    try:
        from tools.env_passthrough import (
            get_all_passthrough,
            resolve_passthrough_value,
        )
        from agent.secret_scope import (
            _is_global_env,
            is_multiplex_active as _is_multiplex_active,
        )
        resolve_value = resolve_passthrough_value
        is_global_env = _is_global_env
        multiplex_active = _is_multiplex_active()
        passthrough_keys = set(get_all_passthrough())
    except Exception:
        pass

    # Imported, not restated: this is the credential boundary from
    # GHSA-rhgp-j443-p4rf, and a fork-local copy that drifted from upstream's
    # would silently reopen it.
    from tools.environments.local import (
        _HERMES_PROVIDER_ENV_BLOCKLIST,
        _is_hermes_internal_secret,
    )

    implicit_forward = {
        k for k in passthrough_keys if not _is_hermes_internal_secret(k)
    }
    forward_keys = explicit_forward_keys | (
        implicit_forward - _HERMES_PROVIDER_ENV_BLOCKLIST
    )
    hermes_env = load_hermes_env_vars() if forward_keys else {}

    unset_names: set[str] = set()
    for key in sorted(forward_keys):
        value = os.getenv(key) or hermes_env.get(key)
        if resolve_value is not None:
            value = resolve_value(key, value)
        if value is not None:
            exec_env[key] = value
        elif multiplex_active and not is_global_env(key) and ENV_VAR_NAME_RE.fullmatch(key):
            unset_names.add(key)
    return exec_env, unset_names


def export_prelude(env: dict[str, str], unset_names=()) -> str:
    """Render *env* and *unset_names* as shell statements to prepend to a script.

    This is the ``docker exec -e`` substitute for backends whose exec
    transport has no env flag. ``kubectl exec`` is the motivating case: it
    passes argv straight through with no way to set variables.

    Values are shell-quoted, and every statement is terminated with a newline
    so a script that opens with a comment or a heredoc cannot swallow them.
    """
    lines: list[str] = []
    if unset_names:
        quoted = " ".join(shlex.quote(name) for name in sorted(unset_names))
        # Unset first: a name can be both stale in the snapshot and absent from
        # the active scope, and the export below must win when it is present.
        lines.append(f"unset {quoted} 2>/dev/null || true")
    for key in sorted(env):
        lines.append(f"export {key}={shlex.quote(env[key])}")
    return "".join(f"{line}\n" for line in lines)
