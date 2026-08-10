"""Unit tests for the Kubernetes pod execution backend.

There is no SDK to stub here — the backend shells out to ``kubectl`` — so the
fake-provider pattern from ``test_daytona_environment.py`` is adapted: every
``subprocess.run`` metadata call and every ``_popen_bash`` exec is intercepted
and recorded so tests can assert on the exact argv.
"""

import io
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.environments.kubernetes import (
    KubernetesEnvironment,
    pod_name_for_task,
)

MODULE = "tools.environments.kubernetes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakeKubectl:
    """Records every kubectl invocation and replies from a scripted table."""

    def __init__(self, pod_phase="Running"):
        self.calls: list[list[str]] = []
        self.stdin_payloads: list[str | None] = []
        self.pod_phase = pod_phase
        # Per-verb overrides: verb -> CompletedProcess
        self.responses: dict[str, subprocess.CompletedProcess] = {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.stdin_payloads.append(kwargs.get("input"))
        key = self._key(cmd)
        if key in self.responses:
            return self.responses[key]
        if key == "get_pod":
            return _completed(stdout=self.pod_phase)
        return _completed()

    @staticmethod
    def _key(cmd):
        """Classify a kubectl invocation.

        ``kubectl get events`` and ``kubectl get pod`` share a verb, so key on
        the resource rather than the verb alone.
        """
        if "events" in cmd:
            return "events"
        if "describe" in cmd:
            return "describe"
        for verb in ("apply", "wait", "delete", "exec", "version"):
            if verb in cmd:
                return verb
        if "get" in cmd:
            return "get_pod"
        return ""

    def calls_with(self, verb):
        return [c for c in self.calls if verb in c]

    def cluster_calls(self):
        """Calls that talk to a cluster (i.e. excluding the client preflight)."""
        return [c for c in self.calls if "--client" not in c]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def kubectl(monkeypatch):
    """Intercept kubectl metadata calls and pretend the binary exists."""
    fake = _FakeKubectl()
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(f"{MODULE}.subprocess.run", fake)
    return fake


@pytest.fixture()
def make_env(kubectl, monkeypatch):
    """Factory building a KubernetesEnvironment with kubectl fully mocked."""
    monkeypatch.setattr("tools.environments.base.is_interrupted", lambda: False)
    # init_session() shells out to build the env snapshot; give it a process
    # that exits cleanly with no output so construction stays hermetic.
    monkeypatch.setattr(
        f"{MODULE}._popen_bash",
        lambda cmd, stdin_data=None, **kw: _make_proc(),
    )

    def _factory(**kwargs):
        kwargs.setdefault("image", "test-image:latest")
        env = KubernetesEnvironment(**kwargs)
        env._kubectl = kubectl  # expose for assertions
        return env

    return _factory


def _make_proc(output="", returncode=0):
    """A minimal ProcessHandle-compatible stand-in for a kubectl exec."""
    proc = MagicMock()
    # A real file object, not a namespace with an ``__iter__`` attribute:
    # ``_wait_for_process`` drains stdout with ``for piece in stream``, and
    # dunder lookup goes through the type, so an instance attribute is never
    # consulted and the output silently comes back empty. StringIO.fileno()
    # raises, which is exactly what routes the drain down the iterable path.
    proc.stdout = io.StringIO(output)
    proc.poll.return_value = returncode
    proc.returncode = returncode
    proc.wait.return_value = returncode
    # _wait_for_process appends any stdin-pipe errors to the output; on a bare
    # MagicMock that attribute auto-creates as truthy and replaces the output.
    proc._hermes_stdin_errors = []
    return proc


# ---------------------------------------------------------------------------
# Pod naming
# ---------------------------------------------------------------------------

class TestPodName:
    def test_deterministic(self):
        assert pod_name_for_task("abc") == pod_name_for_task("abc")

    def test_distinct_tasks_differ(self):
        assert pod_name_for_task("abc") != pod_name_for_task("def")

    @pytest.mark.parametrize("task_id", [
        "default",
        "Session/With Slashes",
        "UPPER_CASE_ID",
        "unicode-ünïcödé",
        "/Users/scott/some/path",
        "a" * 300,
    ])
    def test_dns_1123_safe(self, task_id):
        """Hashing (not sanitizing) is what makes arbitrary task ids safe."""
        name = pod_name_for_task(task_id)
        assert len(name) <= 63
        assert name.islower() or name.replace("-", "").isalnum()
        assert name[0].isalpha() and name[-1].isalnum()
        assert all(c.isalnum() or c == "-" for c in name)

    def test_empty_task_id_falls_back(self):
        assert pod_name_for_task("") == pod_name_for_task("default")


# ---------------------------------------------------------------------------
# Pod creation
# ---------------------------------------------------------------------------

class TestPodCreation:
    def test_running_pod_is_reused(self, make_env, kubectl):
        make_env(task_id="t1")
        assert kubectl.calls_with("apply") == []

    def test_missing_pod_is_created(self, make_env, kubectl):
        kubectl.pod_phase = ""
        make_env(task_id="t1")
        assert kubectl.calls_with("apply")

    def test_manifest_contents(self, make_env, kubectl):
        kubectl.pod_phase = ""
        env = make_env(
            task_id="t1",
            namespace="agents",
            service_account="hermes-runner",
            cpu=2,
            memory=4096,
        )

        idx = kubectl.calls.index(next(c for c in kubectl.calls if "apply" in c))
        manifest = json.loads(kubectl.stdin_payloads[idx])

        assert manifest["metadata"]["name"] == env._pod
        assert manifest["metadata"]["namespace"] == "agents"
        assert manifest["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "hermes"

        spec = manifest["spec"]
        assert spec["serviceAccountName"] == "hermes-runner"
        assert spec["restartPolicy"] == "Never"

        container = spec["containers"][0]
        assert container["image"] == "test-image:latest"
        assert container["command"] == ["sleep", "infinity"]
        assert container["resources"]["limits"] == {"cpu": "2", "memory": "4096Mi"}

    def test_no_resources_key_when_unset(self, make_env, kubectl):
        kubectl.pod_phase = ""
        make_env(task_id="t1", cpu=0, memory=0)
        idx = kubectl.calls.index(next(c for c in kubectl.calls if "apply" in c))
        manifest = json.loads(kubectl.stdin_payloads[idx])
        assert "resources" not in manifest["spec"]["containers"][0]

    def test_terminal_pod_is_replaced(self, make_env, kubectl):
        """restartPolicy=Never means an exited pod blocks the name."""
        kubectl.pod_phase = "Failed"
        make_env(task_id="t1")
        assert kubectl.calls_with("delete")
        assert kubectl.calls_with("apply")

    def test_apply_failure_raises(self, make_env, kubectl):
        kubectl.pod_phase = ""
        kubectl.responses["apply"] = _completed(returncode=1, stderr="forbidden")
        with pytest.raises(RuntimeError, match="forbidden"):
            make_env(task_id="t1")


class TestReadinessFailure:
    def test_wait_failure_includes_diagnostics(self, make_env, kubectl):
        kubectl.pod_phase = ""
        kubectl.responses["wait"] = _completed(returncode=1, stderr="timed out")
        kubectl.responses["describe"] = _completed(
            stdout="Events:\n  Failed to pull image"
        )
        kubectl.responses["events"] = _completed(stdout="Warning  Failed  ErrImagePull")

        with pytest.raises(RuntimeError) as excinfo:
            make_env(task_id="t1")

        message = str(excinfo.value)
        assert "did not become Ready" in message
        # Image-pull failures are invisible in the bare wait error, so the
        # describe/events fold-in is the whole point of this path.
        assert "Failed to pull image" in message
        assert "ErrImagePull" in message


# ---------------------------------------------------------------------------
# kubectl argv construction
# ---------------------------------------------------------------------------

class TestKubectlArgs:
    def test_connection_flags_on_every_call(self, make_env, kubectl):
        kubectl.pod_phase = ""
        make_env(
            task_id="t1",
            namespace="agents",
            context="prod",
            kubeconfig="/tmp/kubeconfig",
            extra_args=["--request-timeout=30s"],
        )
        # The `kubectl version --client` preflight deliberately runs before any
        # connection settings are known, so it is excluded here.
        assert kubectl.cluster_calls()
        for call in kubectl.cluster_calls():
            assert call[:3] == ["kubectl", "--namespace", "agents"]
            assert "--context" in call and "prod" in call
            assert "--kubeconfig" in call and "/tmp/kubeconfig" in call
            assert "--request-timeout=30s" in call

    def test_malformed_extra_args_are_ignored(self, make_env):
        env = make_env(task_id="t1", extra_args="--not-a-list")
        assert env._extra_args == []


class TestRunBash:
    def _capture(self, monkeypatch):
        captured = {}

        def _fake_popen(cmd, stdin_data=None, **kwargs):
            captured["cmd"] = cmd
            captured["stdin"] = stdin_data
            return _make_proc()

        monkeypatch.setattr(f"{MODULE}._popen_bash", _fake_popen)
        return captured

    def test_exec_argv(self, make_env, monkeypatch):
        env = make_env(task_id="t1", namespace="agents")
        captured = self._capture(monkeypatch)

        env._run_bash("echo hi")

        assert captured["cmd"] == [
            "kubectl", "--namespace", "agents",
            "exec", "-i", env._pod, "-c", "hermes", "--",
            "bash", "-c", "echo hi",
        ]

    def test_command_is_not_shell_quoted(self, make_env, monkeypatch):
        """kubectl execs argv directly — quoting would run the literal text.

        This is the single most likely thing to get wrong when porting from
        the SSH backend, which *must* quote because sshd re-parses.
        """
        env = make_env(task_id="t1")
        captured = self._capture(monkeypatch)

        script = "echo 'hello world' && cd /tmp"
        env._run_bash(script)

        assert captured["cmd"][-1] == script

    def test_login_shell_flag(self, make_env, monkeypatch):
        env = make_env(task_id="t1")
        captured = self._capture(monkeypatch)

        env._run_bash("true", login=True)

        assert captured["cmd"][-3:] == ["bash", "-l", "-c"] or \
            captured["cmd"][-4:-1] == ["bash", "-l", "-c"]
        assert "-l" in captured["cmd"]

    def test_stdin_is_piped(self, make_env, monkeypatch):
        env = make_env(task_id="t1")
        captured = self._capture(monkeypatch)

        env._run_bash("cat", stdin_data="payload")

        assert captured["stdin"] == "payload"
        # -i is what makes the pipe reach the container.
        assert "-i" in captured["cmd"]

    def test_stdin_mode_is_pipe(self, make_env):
        env = make_env(task_id="t1")
        assert env._stdin_mode == "pipe"


# ---------------------------------------------------------------------------
# CWD handling
# ---------------------------------------------------------------------------

class TestCwd:
    def test_default_cwd_is_workspace(self, make_env):
        assert make_env(task_id="t1").cwd == "/workspace"

    def test_tilde_cwd_falls_back_to_workspace(self, make_env):
        """``~`` is an SSH-ism; the pod has no meaningful remote home."""
        assert make_env(task_id="t1", cwd="~").cwd == "/workspace"

    def test_explicit_cwd_is_kept(self, make_env):
        assert make_env(task_id="t1", cwd="/srv/app").cwd == "/srv/app"

    def test_cwd_persists_via_base_marker(self, make_env):
        env = make_env(task_id="t1")
        marker = env._cwd_marker
        env._extract_cwd_from_output(
            {"output": f"hi\n{marker}/tmp/newdir{marker}\n", "returncode": 0}
        )
        assert env.cwd == "/tmp/newdir"


# ---------------------------------------------------------------------------
# Recovery from a vanished pod
# ---------------------------------------------------------------------------

class TestPodRecovery:
    """A pod can disappear mid-session; without recovery every later command
    in the task fails forever."""

    @staticmethod
    def _script(monkeypatch, *procs):
        """Serve *procs* to successive _popen_bash calls, then clean exits.

        Installed after construction so the constructor's own init_session()
        does not eat the first scripted entry.
        """
        queue = list(procs)

        def _fake_popen(cmd, stdin_data=None, **kwargs):
            return queue.pop(0) if queue else _make_proc()

        monkeypatch.setattr(f"{MODULE}._popen_bash", _fake_popen)

    def test_recreates_pod_and_retries(self, make_env, kubectl, monkeypatch):
        env = make_env(task_id="t1")
        gone = _make_proc(
            output=f'Error from server (NotFound): pods "{env._pod}" not found',
            returncode=1,
        )
        self._script(monkeypatch, gone, _make_proc(), _make_proc("ok", 0))
        # The pod is really gone, so the API-server confirmation agrees.
        kubectl.pod_phase = ""
        kubectl.calls.clear()

        result = env.execute("echo hi")

        assert kubectl.calls_with("apply"), "pod was not recreated"
        assert result["returncode"] == 0
        assert "ok" in result["output"]

    def test_no_recovery_when_pod_is_still_running(self, make_env, kubectl,
                                                   monkeypatch):
        """The message only nominates a candidate — the API server decides.

        Recreating a pod that is alive would delete a live session's pod.
        """
        env = make_env(task_id="t1")
        self._script(
            monkeypatch,
            _make_proc(
                output=f'Error from server (NotFound): pods "{env._pod}" not found',
                returncode=1,
            ),
        )
        kubectl.pod_phase = "Running"
        kubectl.calls.clear()

        result = env.execute("echo hi")

        assert kubectl.calls_with("apply") == []
        assert result["returncode"] == 1

    def test_ordinary_failure_does_not_touch_the_cluster(self, make_env, kubectl,
                                                         monkeypatch):
        """`command not found` is the most common failing command there is.

        A bare "not found" substring check would recreate the pod on every one.
        """
        env = make_env(task_id="t1")
        self._script(
            monkeypatch,
            _make_proc(output="bash: frobnicate: command not found", returncode=127),
        )
        kubectl.calls.clear()

        result = env.execute("frobnicate")

        assert result["returncode"] == 127
        # Not even the phase lookup should fire: the cheap check gates it.
        assert kubectl.cluster_calls() == []

    def test_failed_recreation_surfaces_the_original_error(self, make_env, kubectl,
                                                           monkeypatch):
        env = make_env(task_id="t1")
        message = f'Error from server (NotFound): pods "{env._pod}" not found'
        self._script(monkeypatch, _make_proc(output=message, returncode=1))
        kubectl.pod_phase = ""
        kubectl.responses["apply"] = _completed(returncode=1, stderr="quota exceeded")
        kubectl.calls.clear()

        result = env.execute("echo hi")

        assert result["returncode"] == 1
        assert message in result["output"]

    @pytest.mark.parametrize("message", [
        'Error from server (NotFound): pods "{pod}" not found',
        "error: cannot exec into a container in a completed pod; "
        "current phase is Failed",
        "error: unable to upgrade connection: container not found (\"hermes\")",
        "Error from server (BadRequest): pod {pod} does not have a host assigned",
    ])
    def test_recognized_kubectl_messages(self, make_env, message):
        env = make_env(task_id="t1")
        assert env._looks_like_pod_gone(message.format(pod=env._pod))

    @pytest.mark.parametrize("message", [
        "bash: frobnicate: command not found",
        "ls: cannot access 'x': No such file or directory",
        'pods "hermes-someothertask" not found',
    ])
    def test_unrelated_messages_are_ignored(self, make_env, message):
        env = make_env(task_id="t1")
        assert not env._looks_like_pod_gone(message)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_deletes_pod_when_not_persistent(self, make_env, kubectl):
        env = make_env(task_id="t1", persistent_filesystem=False)
        kubectl.calls.clear()
        env.cleanup()
        deletes = kubectl.calls_with("delete")
        assert deletes
        assert env._pod in deletes[0]
        assert "--ignore-not-found" in deletes[0]

    def test_keeps_pod_when_persistent(self, make_env, kubectl):
        env = make_env(task_id="t1", persistent_filesystem=True)
        kubectl.calls.clear()
        env.cleanup()
        assert kubectl.calls_with("delete") == []

    def test_delete_failure_is_swallowed(self, make_env, kubectl, monkeypatch):
        env = make_env(task_id="t1", persistent_filesystem=False)
        monkeypatch.setattr(
            f"{MODULE}.subprocess.run",
            MagicMock(side_effect=OSError("boom")),
        )
        env.cleanup()  # must not raise during teardown


# ---------------------------------------------------------------------------
# kubectl availability
# ---------------------------------------------------------------------------

class TestKubectlAvailability:
    def test_missing_kubectl_raises(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: None)
        with pytest.raises(RuntimeError, match="kubectl is not installed"):
            KubernetesEnvironment(image="x")

    def test_broken_client_raises(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: "/usr/bin/kubectl")
        monkeypatch.setattr(
            f"{MODULE}.subprocess.run",
            lambda *a, **kw: _completed(returncode=1, stderr="bad config"),
        )
        with pytest.raises(RuntimeError, match="bad config"):
            KubernetesEnvironment(image="x")
