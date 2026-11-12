"""The host enforces the persisted no-tools boundary for local skill turns."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


def local_policy(tmp_path):
    from gateway.session_policy import build_policy

    return build_policy(
        {"source": "cli", "cwd": str(tmp_path), "model": "fixture-model", "local_only": True},
        {}, profile_terminal=False,
    )


def test_local_only_policy_survives_restore_with_no_tools_or_following(tmp_path):
    from gateway.session_policy import restore_policy

    policy = restore_policy(asdict(local_policy(tmp_path)))
    assert policy.local_only is True
    assert policy.safe_mode is True
    assert policy.ignore_user_config is True
    assert policy.toolsets == ()
    assert policy.editor_mcp_json is None
    assert '"local_only": true' in policy.request_json
    assert '"follow_profile_config": true' not in policy.request_json


def test_local_only_rejects_conflicting_create_options(tmp_path):
    from gateway.session_policy import build_policy
    from persistence.core.errors import AuthorityRefusal

    for conflict in ({"accept_hooks": True}, {"follow_profile_config": True}, {"editor": {"server": "x"}}):
        with pytest.raises(AuthorityRefusal):
            build_policy({"cwd": str(tmp_path), "model": "fixture-model", "local_only": True, **conflict}, {},
                         profile_terminal=False)


def test_unexpected_model_tool_call_does_not_reach_registered_handler(tmp_path):
    from gateway.session_policy import policy_scope
    from tools.registry import registry
    import model_tools

    called = []
    name = "local_only_policy_probe"
    registry.register(name=name, toolset="safe", schema={"name": name, "parameters": {"type": "object"}},
                      handler=lambda args, **kwargs: called.append(args) or "executed")
    try:
        with policy_scope(local_policy(tmp_path)):
            result = model_tools.handle_function_call(name, {})
        assert "disabled for this local-only session" in result
        assert called == []
    finally:
        registry.deregister(name)


def test_inline_tool_dispatch_and_plugin_hooks_are_suppressed(tmp_path, monkeypatch):
    from gateway.session_policy import policy_scope
    from agent.tool_executor import _dispatch_authorized_once
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManager

    called = []
    manager = PluginManager(scope_key=str(tmp_path))
    manager._hooks["pre_tool_call"] = [lambda **kwargs: called.append("hook")]
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    state = SimpleNamespace(blocked=False)
    ref = SimpleNamespace(name="memory", args={}, trace=[], emit_post=lambda *args, **kwargs: None)
    with policy_scope(local_policy(tmp_path)):
        assert plugins.invoke_hook("pre_tool_call", tool_name="memory") == []
        result = _dispatch_authorized_once(
            None, state, ref, execute=lambda args: called.append("inline"), scope_block=None,
            display_index=None, begin_execution=None, authorization_gate=None,
        )
    assert "disabled for this local-only session" in result
    assert state.blocked is True
    assert called == []


def test_managed_worker_keeps_local_only_tool_guard_in_fresh_process(tmp_path):
    frame = {
        "version": 1, "home": str(tmp_path),
        "scope": {"profile_id": str(tmp_path), "session_id": "session", "execution_id": "execution",
                  "generation": 1, "pid": 0, "birth": "birth", "secret": "secret", "epoch": 1},
        "policy": {"config_json": "{}", "request_json": '{"local_only":true}', "local_only": True,
                   "toolsets": [], "editor_mcp_json": None},
        "api_key": None, "text": "run a skill", "route": "route", "user_id": "user", "chat_id": "chat",
        "turn_author": None, "safe_mode": True, "ignore_user_config": True, "local_only": True,
    }
    child_code = r'''
import json, os, sys
from agent.managed_worker import bind_worker_policy, validate_bootstrap
frame = json.loads(sys.argv[1])
frame["scope"]["pid"] = os.getpid()
validate_bootstrap(frame)
bind_worker_policy(frame)
from agent.local_execution_policy import local_only_execution
assert local_only_execution()
from tools.registry import registry
import model_tools
called = []
name = "local_only_worker_probe"
registry.register(name=name, toolset="safe", schema={"name": name, "parameters": {"type": "object"}},
                  handler=lambda args, **kwargs: called.append(args) or "executed")
result = model_tools.handle_function_call(name, {})
assert "disabled for this local-only session" in result
assert called == []
'''
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    completed = subprocess.run([sys.executable, "-c", child_code, json.dumps(frame)],
                               cwd=str(Path(__file__).resolve().parents[2]), env=env,
                               capture_output=True, text=True, timeout=30,
                               check=False)
    assert completed.returncode == 0, completed.stderr or completed.stdout
