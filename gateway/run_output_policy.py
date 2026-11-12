"""Profile runtime binding for outbound transport policy."""

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Iterator
import asyncio

from ruamel.yaml.error import YAMLError

from gateway.output_policy import (
    OutputPolicy,
    bind_output_policy,
    current_output_policy,
    profile_output_policy,
    register_protected_profile,
    reset_output_policy,
)


@contextmanager
def profile_output_policy_scope(profile_home: Path) -> Iterator[None]:
    """Resolve and bind output policy while the profile home override is active."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(str(profile_home))
    try:
        current = current_output_policy()
        if isinstance(current, OutputPolicy) and current.profile_home == str(profile_home):
            policy = current
        else:
            policy = _resolve_profile_output_policy(Path(profile_home), load_user_config_effective)
        token = bind_output_policy(policy)
        try:
            yield
        finally:
            reset_output_policy(token)
    finally:
        reset_hermes_home_override(home_token)


def _marker_path(profile_home: Path) -> Path:
    return Path(profile_home) / "security" / "output_policy.json"


def _persist_protection_marker(profile_home: Path) -> None:
    """Persist the first deny decision so deleting the config key cannot reopen output on restart."""
    marker = _marker_path(profile_home)
    from hermes_constants import mkdir_under_hermes_home
    mkdir_under_hermes_home(marker.parent)
    if marker.exists():
        return
    from utils import atomic_write_text

    atomic_write_text(
        marker,
        json.dumps({"version": 1, "platforms": {"slack": "deny"}}) + "\n",
        tmp_prefix=".output-policy-",
        mode=0o600,
        fsync_dir=True,
    )


def _activate_profile_policy(profile_home: Path, config: dict) -> None:
    marker = _marker_path(profile_home)
    try:
        has_marker = marker.exists()
    except OSError:
        register_protected_profile(profile_home)
        raise RuntimeError(f"Cannot inspect persisted output policy for profile {profile_home}")
    if has_marker:
        register_protected_profile(profile_home)
    policy = profile_output_policy(profile_home, config)
    if policy is not None and policy.slack == "deny":
        register_protected_profile(profile_home)
        _persist_protection_marker(profile_home)


def _resolve_profile_output_policy(profile_home: Path, loader=None):
    """Resolve effective policy under its owning home; safe to call from a worker thread."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(profile_home))
    try:
        config = (loader or load_user_config_effective)(
            Path(profile_home) / "config.yaml", fail_closed=True)
        _activate_profile_policy(Path(profile_home), config)
        return profile_output_policy(Path(profile_home), config)
    finally:
        reset_hermes_home_override(token)


async def resolved_profile_output_policy(profile_home: Path) -> OutputPolicy | None:
    """Resolve one profile's policy off-loop, reusing its immutable bound snapshot when present."""
    current = current_output_policy()
    if isinstance(current, OutputPolicy) and current.profile_home == str(profile_home):
        return current
    try:
        return await asyncio.to_thread(_resolve_profile_output_policy, Path(profile_home))
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, SystemExit, YAMLError):
        return OutputPolicy(str(profile_home), "deny")


async def protected_slack_observe_only(
    runner, event, source=None, *, run_hook: bool = False,
) -> bool:
    """Offer protected Slack input to intake once, then refuse ordinary dispatch."""
    from gateway.config import Platform
    from gateway.output_policy import (
        OutputPolicy,
        bind_output_policy,
        current_output_policy,
        profile_output_denied,
        reset_output_policy,
        slack_output_denied_for_unknown_profile,
    )

    source = source or event.source
    if getattr(getattr(source, "platform", None), "value", None) != Platform.SLACK.value:
        return False
    if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        runner._canonicalize(source)
    try:
        profile_home = runner._resolve_profile_home_for_source(source)
    except (AttributeError, KeyError, RuntimeError, ValueError):
        profile_home = None
    policy = None
    if profile_home is None:
        denied = slack_output_denied_for_unknown_profile()
    else:
        profile_home = Path(profile_home)
        policy = await resolved_profile_output_policy(profile_home)
        denied = profile_output_denied(policy)
    if not denied:
        return False
    if run_hook:
        if profile_home is not None and isinstance(policy, OutputPolicy):
            token = bind_output_policy(policy)
            try:
                from gateway.run import _async_profile_runtime_scope
                async with _async_profile_runtime_scope(profile_home):
                    await runner._hm_pre_gateway_dispatch_hook(event, source)
            finally:
                reset_output_policy(token)
        else:
            await runner._hm_pre_gateway_dispatch_hook(event, source)
    return True


async def admit_busy_message(adapter, event, session_key: str) -> bool:
    """Handle protected Slack input before active-session commands or the shared FIFO."""
    source = event.source
    runner = getattr(adapter, "gateway_runner", None)
    if getattr(getattr(source, "platform", None), "value", None) == "slack" and runner is not None:
        if await protected_slack_observe_only(runner, event, source, run_hook=True):
            event._gateway_accepted = True
            return True
    if adapter._shared_runtime_owns_input(event):
        from gateway.session_ingress_burst import dispatch_busy_burst
        await dispatch_busy_burst(adapter, event, session_key)
        return True
    return False


def register_output_policy_roster(profile_homes: list[tuple[str, Path]]) -> None:
    """Load and latch all served profiles before adapters can publish status or recover delivery."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    for _name, home in profile_homes:
        token = set_hermes_home_override(str(home))
        try:
            _activate_profile_policy(
                Path(home), load_user_config_effective(Path(home) / "config.yaml", fail_closed=True))
        finally:
            reset_hermes_home_override(token)
