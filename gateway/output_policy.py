"""Profile-scoped policy for host-managed outbound transports.

This is a transport guard. It does not constrain direct network access by handlers or processes.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ruamel.yaml.error import YAMLError


SlackDecision = Literal["allow", "deny"]


@dataclass(frozen=True)
class OutputPolicy:
    profile_home: str
    slack: SlackDecision


_UNBOUND = object()
_current_policy: ContextVar[OutputPolicy | None | object] = ContextVar(
    "hermes_output_policy", default=_UNBOUND)
_protected_profiles: set[str] = set()


def register_protected_profile(profile_home: str | Path) -> None:
    """Latch a protected profile for this process so unattributed shared writes fail closed."""
    _protected_profiles.add(str(profile_home))


def bind_output_policy(policy: OutputPolicy | None) -> Token:
    """Bind the resolved policy for one profile activity; ``None`` means explicit allow."""
    return _current_policy.set(policy)


def reset_output_policy(token: Token) -> None:
    _current_policy.reset(token)


def current_output_policy() -> OutputPolicy | None | object:
    """Return policy, explicit allow (None), or the private unbound sentinel."""
    return _current_policy.get()


def profile_output_denied(policy: OutputPolicy | None) -> bool:
    """Check a resolved profile snapshot against its immutable deny decision and process latch."""
    return policy is None or policy.slack != "allow" or policy.profile_home in _protected_profiles


def slack_output_denied_for_unknown_profile() -> bool:
    """Unknown source attribution fails closed whenever any protected profile is served."""
    return bool(_protected_profiles) or slack_output_denied()


def slack_output_denied(owner_policy: OutputPolicy | None = None) -> bool:
    """Fail closed for malformed/unknown decisions and resolve process policy when unbound."""
    policy = _current_policy.get()
    is_bound = policy is not _UNBOUND
    if is_bound and isinstance(policy, OutputPolicy):
        if profile_output_denied(policy):
            return True
    if owner_policy is not None and profile_output_denied(owner_policy):
        return True
    if owner_policy is not None and owner_policy.slack != "allow":
        return True
    if not is_bound:
        if _protected_profiles:
            return True
        policy = owner_policy
    if policy is None and not is_bound:
        try:
            from hermes_constants import get_hermes_home
            from hermes_cli.config_effective import load_user_config_effective

            home = get_hermes_home()
            policy = profile_output_policy(
                Path(home), load_user_config_effective(Path(home) / "config.yaml", fail_closed=True))
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, SystemExit, YAMLError):
            # A policy read failure must not permit a message through the transport.
            return True
    if policy is None:
        return False
    return not isinstance(policy, OutputPolicy) or policy.slack != "allow"


def profile_output_policy(profile_home: Path, config: dict) -> OutputPolicy | None:
    """Resolve a profile's effective policy. Unconfigured profiles retain legacy behavior.

    Any configured value other than the exact supported values is represented as deny, so malformed
    policy cannot silently weaken a guard. The caller is responsible for refusing activation when a
    host marks the profile protected and this key is absent.
    """
    profile_key = str(profile_home)
    if profile_key in _protected_profiles:
        return OutputPolicy(profile_key, "deny")
    security = config.get("security")
    if not isinstance(security, dict):
        decision: SlackDecision = "deny" if "security" in config else "allow"
        return OutputPolicy(str(profile_home), decision)
    section = security.get("output_policy")
    if "output_policy" not in security:
        decision = "deny" if str(profile_home) in _protected_profiles else "allow"
        return OutputPolicy(str(profile_home), decision)
    if not isinstance(section, dict) or "slack" not in section:
        return OutputPolicy(str(profile_home), "deny")
    value = section["slack"]
    decision: SlackDecision = "allow" if value == "allow" else "deny"
    return OutputPolicy(str(profile_home), decision)
