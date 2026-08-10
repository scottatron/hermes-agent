"""Kubernetes pod execution environment driven by the ``kubectl`` CLI.

Hermes owns the pod lifecycle: a ``sleep infinity`` pod is created on first use
from a deterministic, task-derived name, every command runs through
``kubectl exec``, and the pod is deleted on cleanup unless persistence is on.

Because ``kubectl exec`` is an ordinary subprocess, ``_run_bash()`` returns a
real :class:`subprocess.Popen` and the base class's timeout / interrupt / kill
machinery works unchanged — no threaded handle adapter is needed.

Known limitations (see also the Hermes tools docs):

* No file synchronization. ``~/.hermes`` credentials, skills, and cache are
  **not** present in the pod, so credential-dependent skills will fail there.
* ``terminal.cwd`` is pod-local. The host working tree is neither mounted nor
  synced, so a command can succeed in the pod while the host checkout is
  untouched.
* The workspace is an ``emptyDir`` and dies with the pod.
  ``container_persistent`` only keeps the *pod* alive; it is not a PVC.
* PTY mode is unsupported (the terminal schema already restricts it to
  local/SSH).
* Cancellation kills the local ``kubectl`` client, not the in-pod process tree.
  A child that ignores the closed stream can outlive a timeout.

Required RBAC in the target namespace: create/get/delete on ``pods``, create on
``pods/exec``, get on ``pods/log``, and list on ``events``.
"""

import hashlib
import json
import logging
import shutil
import subprocess

from tools.environments.base import BaseEnvironment, _popen_bash

logger = logging.getLogger(__name__)

# Name of the single container inside the Hermes-managed pod.
POD_CONTAINER_NAME = "hermes"

# Label applied to every pod Hermes creates, so orphans stay findable with
# ``kubectl get pods -l app.kubernetes.io/managed-by=hermes``.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "hermes"
TASK_ID_LABEL = "hermes.nousresearch.com/task-id"

DEFAULT_WORKSPACE = "/workspace"

# Short timeouts for metadata calls; command execution uses the caller's budget.
_METADATA_TIMEOUT = 30


def _ensure_kubectl_available() -> None:
    """Fail fast with a clear error when kubectl is unusable."""
    if not shutil.which("kubectl"):
        raise RuntimeError(
            "kubectl is not installed or not in PATH. Install it from "
            "https://kubernetes.io/docs/tasks/tools/ or via your package manager "
            "(e.g. brew install kubectl)."
        )
    try:
        result = subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"'kubectl version --client' could not be run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            "kubectl is present but 'kubectl version --client' failed: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def pod_name_for_task(task_id: str) -> str:
    """Return a deterministic, DNS-1123-safe pod name for *task_id*.

    Hashing rather than sanitizing means arbitrary task ids (session UUIDs,
    paths, unicode) always produce a valid name, and the same task maps to the
    same pod across Hermes processes so a restart reattaches instead of
    orphaning the old pod.
    """
    digest = hashlib.sha256((task_id or "default").encode("utf-8")).hexdigest()[:16]
    return f"hermes-{digest}"


class KubernetesEnvironment(BaseEnvironment):
    """Run commands inside a Hermes-managed Kubernetes pod via ``kubectl exec``.

    Spawn-per-call: every execute() spawns a fresh ``kubectl exec ... bash -c``
    process. Session snapshot preserves env vars across calls and CWD persists
    via in-band stdout markers, both handled by :class:`BaseEnvironment`.
    """

    def __init__(
        self,
        image: str,
        cwd: str = DEFAULT_WORKSPACE,
        timeout: int = 60,
        namespace: str = "default",
        context: str = "",
        kubeconfig: str = "",
        service_account: str = "",
        cpu: float = 0,
        memory: int = 0,
        task_id: str = "default",
        persistent_filesystem: bool = True,
        pod_ready_timeout: int = 120,
        extra_args: list | None = None,
    ):
        if not cwd or cwd == "~":
            cwd = DEFAULT_WORKSPACE
        super().__init__(cwd=cwd, timeout=timeout)

        self._image = image
        self._namespace = namespace or "default"
        self._context = context or ""
        self._kubeconfig = kubeconfig or ""
        self._service_account = service_account or ""
        self._cpu = cpu
        self._memory = memory
        self._task_id = task_id or "default"
        self._persistent = persistent_filesystem
        self._pod_ready_timeout = max(1, int(pod_ready_timeout or 120))

        # Extra args are appended to every kubectl invocation. Tolerate a
        # malformed config.yaml value rather than crashing every command.
        if extra_args is not None and not isinstance(extra_args, list):
            logger.warning("k8s_extra_args config is not a list: %r", extra_args)
            extra_args = []
        self._extra_args = [str(a) for a in (extra_args or [])]

        self._pod = pod_name_for_task(self._task_id)

        _ensure_kubectl_available()
        self._ensure_pod()
        self.init_session()

    # ------------------------------------------------------------------
    # kubectl plumbing
    # ------------------------------------------------------------------

    def _base_kubectl_cmd(self) -> list:
        """Return the kubectl prefix carrying namespace/context/kubeconfig.

        Every kubectl call goes through here so connection settings can never
        drift between pod creation and command execution.
        """
        cmd = ["kubectl", "--namespace", self._namespace]
        if self._context:
            cmd.extend(["--context", self._context])
        if self._kubeconfig:
            cmd.extend(["--kubeconfig", self._kubeconfig])
        cmd.extend(self._extra_args)
        return cmd

    def _run_kubectl(self, args: list, *, timeout: int = _METADATA_TIMEOUT,
                     stdin_data: str | None = None) -> subprocess.CompletedProcess:
        """Run a short-lived kubectl metadata command and capture its output."""
        return subprocess.run(
            self._base_kubectl_cmd() + args,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            input=stdin_data,
            stdin=None if stdin_data is not None else subprocess.DEVNULL,
        )

    # ------------------------------------------------------------------
    # Pod lifecycle
    # ------------------------------------------------------------------

    def _pod_phase(self) -> str:
        """Return the pod's phase, or "" when it does not exist."""
        try:
            result = self._run_kubectl(
                ["get", "pod", self._pod, "-o", "jsonpath={.status.phase}"]
            )
        except subprocess.SubprocessError:
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    def _build_pod_manifest(self) -> dict:
        container: dict = {
            "name": POD_CONTAINER_NAME,
            "image": self._image,
            # Keep the pod alive so it behaves like a long-lived shell host;
            # commands arrive later via `kubectl exec`.
            "command": ["sleep", "infinity"],
            "workingDir": self.cwd,
            "volumeMounts": [{"name": "workspace", "mountPath": DEFAULT_WORKSPACE}],
        }

        limits: dict = {}
        if self._cpu and self._cpu > 0:
            limits["cpu"] = str(self._cpu)
        if self._memory and self._memory > 0:
            limits["memory"] = f"{int(self._memory)}Mi"
        if limits:
            container["resources"] = {"limits": limits, "requests": dict(limits)}

        spec: dict = {
            "restartPolicy": "Never",
            "containers": [container],
            "volumes": [{"name": "workspace", "emptyDir": {}}],
        }
        if self._service_account:
            spec["serviceAccountName"] = self._service_account

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self._pod,
                "namespace": self._namespace,
                "labels": {
                    MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                    TASK_ID_LABEL: self._pod.removeprefix("hermes-"),
                },
            },
            "spec": spec,
        }

    def _ensure_pod(self) -> None:
        """Create the pod if needed, then block until it is Ready."""
        phase = self._pod_phase()
        if phase == "Running":
            logger.info("Kubernetes: reusing pod %s/%s", self._namespace, self._pod)
            return

        if phase in ("Succeeded", "Failed"):
            # A terminal pod can't be exec'd into and blocks the name; a
            # restartPolicy=Never pod that exited is the common way to get here.
            logger.info(
                "Kubernetes: replacing pod %s in terminal phase %s", self._pod, phase
            )
            self._delete_pod()

        if phase != "Pending":
            manifest = json.dumps(self._build_pod_manifest())
            result = self._run_kubectl(["apply", "-f", "-"], stdin_data=manifest)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to create pod {self._namespace}/{self._pod}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )

        self._wait_for_ready()

    def _wait_for_ready(self) -> None:
        try:
            result = self._run_kubectl(
                [
                    "wait",
                    f"pod/{self._pod}",
                    "--for=condition=Ready",
                    f"--timeout={self._pod_ready_timeout}s",
                ],
                timeout=self._pod_ready_timeout + 15,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(self._pod_failure_message("kubectl wait timed out"))
        if result.returncode != 0:
            raise RuntimeError(
                self._pod_failure_message((result.stderr or result.stdout).strip())
            )

    def _pod_failure_message(self, reason: str) -> str:
        """Build a diagnosable error for a pod that never became Ready.

        Image-pull and scheduling failures are the dominant first-run failure,
        and both are invisible in the bare wait error — so fold `describe` and
        the namespace events into the message the user actually sees.
        """
        details = []
        for label, args in (
            ("describe", ["describe", "pod", self._pod]),
            (
                "events",
                ["get", "events", "--field-selector",
                 f"involvedObject.name={self._pod}"],
            ),
        ):
            try:
                out = self._run_kubectl(args, timeout=15)
                text = (out.stdout or "").strip()
                if text:
                    details.append(f"--- kubectl {label} ---\n{text}")
            except (OSError, subprocess.SubprocessError):
                continue

        message = (
            f"Kubernetes pod {self._namespace}/{self._pod} did not become Ready "
            f"within {self._pod_ready_timeout}s: {reason}"
        )
        if details:
            message += "\n" + "\n".join(details)
        return message

    def _delete_pod(self) -> None:
        try:
            self._run_kubectl(
                ["delete", "pod", self._pod, "--wait=false", "--ignore-not-found"]
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Kubernetes: failed to delete pod %s: %s", self._pod, exc)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn a kubectl process that runs bash inside the pod.

        Unlike the SSH backend, *cmd_string* is passed unquoted: sshd re-parses
        its remote command through a shell (hence ``shlex.quote`` there), but
        ``kubectl exec -- bash -c`` execs the argv array directly, so quoting
        would make bash run the literal quoted text.
        """
        cmd = self._base_kubectl_cmd() + [
            "exec", "-i", self._pod, "-c", POD_CONTAINER_NAME, "--",
        ]
        if login:
            cmd.extend(["bash", "-l", "-c", cmd_string])
        else:
            cmd.extend(["bash", "-c", cmd_string])

        return _popen_bash(cmd, stdin_data)

    def cleanup(self):
        if self._persistent:
            logger.info(
                "Kubernetes: keeping pod %s/%s (container_persistent is enabled)",
                self._namespace, self._pod,
            )
            return
        logger.info("Kubernetes: deleting pod %s/%s", self._namespace, self._pod)
        self._delete_pod()
