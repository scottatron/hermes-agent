import importlib
import logging
import subprocess

import pytest


terminal_tool_module = importlib.import_module("tools.terminal_tool")


def _clear_terminal_env(monkeypatch):
    """Remove terminal env vars that could affect requirements checks."""
    keys = [
        "TERMINAL_ENV",
        "TERMINAL_CONTAINER_CPU",
        "TERMINAL_CONTAINER_DISK",
        "TERMINAL_CONTAINER_MEMORY",
        "TERMINAL_DOCKER_FORWARD_ENV",
        "TERMINAL_DOCKER_VOLUMES",
        "TERMINAL_LIFETIME_SECONDS",
        "TERMINAL_MODAL_MODE",
        "TERMINAL_SSH_HOST",
        "TERMINAL_SSH_PORT",
        "TERMINAL_SSH_USER",
        "TERMINAL_TIMEOUT",
        "TERMINAL_VERCEL_RUNTIME",
        "TERMINAL_K8S_NAMESPACE",
        "TERMINAL_K8S_CONTEXT",
        "TERMINAL_K8S_KUBECONFIG",
        "TERMINAL_K8S_EXTRA_ARGS",
        "TERMINAL_K8S_POD_READY_TIMEOUT",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
        "HOME",
        "USERPROFILE",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    # Default: no Nous subscription — patch both the terminal_tool local
    # binding and tool_backend_helpers (used by resolve_modal_backend_state).
    monkeypatch.setattr(terminal_tool_module, "managed_nous_tools_enabled", lambda: False)
    import tools.tool_backend_helpers as _tbh
    monkeypatch.setattr(_tbh, "managed_nous_tools_enabled", lambda: False)


def test_local_terminal_requirements(monkeypatch, caplog):
    """Local backend uses Hermes' own LocalEnvironment wrapper."""
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "local")

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is True
    assert "Terminal requirements check failed" not in caplog.text


def test_unknown_terminal_env_logs_error_and_returns_false(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "unknown-backend")

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "Unknown TERMINAL_ENV 'unknown-backend'" in record.getMessage()
        for record in caplog.records
    )


def test_modal_backend_managed_mode_without_feature_flag_logs_clear_error(monkeypatch, caplog, tmp_path):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "modal")
    monkeypatch.setenv("TERMINAL_MODAL_MODE", "managed")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(terminal_tool_module, "is_managed_tool_gateway_ready", lambda _vendor: False)

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "Nous Tool Gateway access is not currently available" in record.getMessage()
        for record in caplog.records
    )


def test_vercel_backend_without_sdk_logs_specific_error(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: None)

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "vercel is required for the Vercel Sandbox terminal backend" in record.getMessage()
        for record in caplog.records
    )


def test_vercel_backend_without_auth_logs_specific_error(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "no supported auth configuration was found" in record.getMessage()
        for record in caplog.records
    )


def test_vercel_backend_accepts_oidc_auth(monkeypatch):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    assert terminal_tool_module.check_terminal_requirements() is True


def test_vercel_backend_accepts_token_tuple_auth(monkeypatch):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("VERCEL_TOKEN", "token")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "project")
    monkeypatch.setenv("VERCEL_TEAM_ID", "team")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    assert terminal_tool_module.check_terminal_requirements() is True


@pytest.mark.parametrize("runtime", ["node24", "node22", "python3.13"])
def test_vercel_backend_accepts_supported_runtimes(monkeypatch, runtime):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("TERMINAL_VERCEL_RUNTIME", runtime)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    assert terminal_tool_module.check_terminal_requirements() is True


def test_vercel_backend_accepts_blank_runtime(monkeypatch):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("TERMINAL_VERCEL_RUNTIME", "   ")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    assert terminal_tool_module.check_terminal_requirements() is True


def test_vercel_backend_rejects_unsupported_runtime(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("TERMINAL_VERCEL_RUNTIME", "node20")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "Vercel Sandbox runtime 'node20' is not supported" in record.getMessage()
        and "node24, node22, python3.13" in record.getMessage()
        for record in caplog.records
    )


def test_vercel_backend_rejects_nondefault_disk(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("TERMINAL_CONTAINER_DISK", "8192")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "does not support custom TERMINAL_CONTAINER_DISK=8192" in record.getMessage()
        for record in caplog.records
    )


def test_vercel_backend_rejects_malformed_disk_without_raising(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "vercel_sandbox")
    monkeypatch.setenv("TERMINAL_CONTAINER_DISK", "large")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
    monkeypatch.setattr(terminal_tool_module.importlib.util, "find_spec", lambda _name: object())

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "Invalid value for TERMINAL_CONTAINER_DISK" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Kubernetes backend
# ---------------------------------------------------------------------------

def _stub_kubectl(monkeypatch, *, present=True, version_rc=0, can_i="yes",
                  can_i_raises=False):
    """Stub kubectl discovery and both subprocess probes; return recorded calls."""
    calls: list[list[str]] = []

    monkeypatch.setattr(
        terminal_tool_module.shutil, "which",
        lambda name: "/usr/bin/kubectl" if (present and name == "kubectl") else None,
    )

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "version" in cmd:
            return subprocess.CompletedProcess(cmd, version_rc, "", "client error")
        if can_i_raises:
            raise OSError("no cluster")
        return subprocess.CompletedProcess(cmd, 0, can_i, "")

    monkeypatch.setattr(terminal_tool_module.subprocess, "run", _fake_run)
    return calls


def test_kubernetes_backend_requires_kubectl(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "kubernetes")
    _stub_kubectl(monkeypatch, present=False)

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any("kubectl not found in PATH" in r.getMessage() for r in caplog.records)


def test_kubernetes_backend_rejects_broken_client(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "kubernetes")
    _stub_kubectl(monkeypatch, version_rc=1)

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any("kubectl version --client" in r.getMessage() for r in caplog.records)


def test_kubernetes_backend_accepts_permitted_namespace(monkeypatch):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "kubernetes")
    monkeypatch.setenv("TERMINAL_K8S_NAMESPACE", "agents")
    calls = _stub_kubectl(monkeypatch, can_i="yes")

    assert terminal_tool_module.check_terminal_requirements() is True

    auth = next(c for c in calls if "can-i" in c)
    assert auth[-2:] == ["--namespace", "agents"]


def test_kubernetes_backend_rejects_denied_rbac(monkeypatch, caplog):
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "kubernetes")
    _stub_kubectl(monkeypatch, can_i="no")

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any("cannot " in r.getMessage() and "create pods" in r.getMessage()
               for r in caplog.records)


def test_kubernetes_backend_tolerates_unreachable_cluster(monkeypatch):
    """An unreachable API server is not a hard 'no' — let pod creation report it."""
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "kubernetes")
    _stub_kubectl(monkeypatch, can_i_raises=True)

    assert terminal_tool_module.check_terminal_requirements() is True
