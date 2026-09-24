"""Contract test: tui_gateway._set_session_context must inject the live
session id into HERMES_SESSION_ID so terminal/execute_code subprocesses can
read the current session's id.

Regression for the bug where _set_session_context called set_session_vars
WITHOUT session_id, leaving the contextvar as "" (explicitly empty). Because
the session-context bridge treats an explicit "" as authoritative and does NOT
fall back to os.environ, every terminal command in a dashboard/TUI/web session
saw an empty HERMES_SESSION_ID even though agent_init had set it via
set_current_session_id().
"""
import pytest
from pathlib import Path

from gateway.session_context import (
    get_session_env,
    _VAR_MAP,
    _UNSET,
)
import tui_gateway.server as server


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Reset all session contextvars to _UNSET between tests.

    In production each asyncio.Task/worker thread gets a fresh context copy
    where the defaults are _UNSET. In tests functions share one thread
    context, so a value set by test A would leak into test B without this.
    """
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)


class _FakeAgent:
    def __init__(self, session_id):
        self.session_id = session_id


def _install_session(
    monkeypatch,
    *,
    session_key,
    agent_session_id,
    source="cli",
    profile_home=None,
):
    """Register a fake session in server._sessions for the duration of a test."""
    sess = {
        "session_key": session_key,
        "source": source,
        "agent": _FakeAgent(agent_session_id) if agent_session_id is not None else None,
        "cwd": "/home/user",
    }
    if profile_home is not None:
        sess["profile_home"] = str(profile_home)
    monkeypatch.setattr(server, "_sessions", {session_key: sess}, raising=False)
    return sess


def test_set_session_context_injects_agent_session_id(monkeypatch):
    """HERMES_SESSION_ID must equal the live agent.session_id after binding."""
    _install_session(
        monkeypatch, session_key="skey-abc", agent_session_id="20260722_deadbeef"
    )

    server._set_session_context("skey-abc", ui_session_id="ui-123")

    assert get_session_env("HERMES_SESSION_ID") == "20260722_deadbeef"


def test_set_session_context_falls_back_to_session_key(monkeypatch):
    """When the agent has no session_id yet, fall back to the session_key
    (never leave HERMES_SESSION_ID empty for an identified session)."""
    _install_session(monkeypatch, session_key="skey-xyz", agent_session_id=None)

    server._set_session_context("skey-xyz")

    assert get_session_env("HERMES_SESSION_ID") == "skey-xyz"


def test_set_session_context_injects_session_profile(tmp_path, monkeypatch):
    """A non-launch Desktop session exports its own profile name to tools."""
    profile_home = tmp_path / "profiles" / "work"
    _install_session(
        monkeypatch,
        session_key="skey-work",
        agent_session_id="session-work",
        profile_home=profile_home,
    )

    server._set_session_context("skey-work")

    assert get_session_env("HERMES_SESSION_PROFILE") == "work"


def test_set_session_context_uses_launch_profile_without_override(monkeypatch):
    """Launch-profile sessions also receive a non-empty profile identity."""
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    _install_session(
        monkeypatch,
        session_key="skey-default",
        agent_session_id="session-default",
    )

    server._set_session_context("skey-default")

    assert get_session_env("HERMES_SESSION_PROFILE") == "default"


def test_symlinked_launch_profile_flows_to_terminal_identity(tmp_path, monkeypatch):
    """TUI and Desktop launch sessions retain the profile name through container routing."""
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import terminal_tool
    from tools.environments.docker import _container_identity

    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    target = tmp_path / "external-profile"
    target.mkdir()
    (root / "profiles" / "stunt-double").symlink_to(target, target_is_directory=True)
    other_target = tmp_path / "other-external-profile"
    other_target.mkdir()
    other_home = root / "profiles" / "scout"
    other_home.symlink_to(other_target, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "stunt-double"))
    monkeypatch.setattr(terminal_tool, "_session_scope", lambda: terminal_tool._SessionScope("docker", True))
    monkeypatch.setattr(terminal_tool, "_tenv", lambda name, default="": default)
    _install_session(monkeypatch, session_key="skey-linked", agent_session_id="session-linked", source="desktop")

    assert get_active_profile_name() == "stunt-double"
    server._set_session_context("skey-linked")
    assert get_session_env("HERMES_SESSION_PROFILE") == "stunt-double"
    assert terminal_tool._resolve_container_task_id("session-linked") == "profile:stunt-double"
    assert _container_identity() == "stunt-double"

    _install_session(monkeypatch, session_key="skey-other", agent_session_id="session-other",
                     source="desktop", profile_home=other_home)
    token = set_hermes_home_override(str(other_home))
    try:
        assert get_active_profile_name() == "scout"
        server._set_session_context("skey-other")
        assert terminal_tool._resolve_container_task_id("session-other") == "profile:scout"
        assert _container_identity() == "scout"
    finally:
        reset_hermes_home_override(token)

    _install_session(monkeypatch, session_key="skey-linked", agent_session_id="session-linked", source="tui")
    assert get_active_profile_name() == "stunt-double"
    server._set_session_context("skey-linked")
    assert terminal_tool._resolve_container_task_id("session-linked") == "profile:stunt-double"
    assert _container_identity() == "stunt-double"
