"""Unit tests for shared host-to-sandbox environment forwarding.

The parity test at the bottom is the important one: this module restates
logic that ``tools/environments/docker.py`` owns privately, and the thing that
must never drift is which variables are eligible for forwarding.
"""

from types import SimpleNamespace

import pytest

from tools.environments.env_forwarding import (
    export_prelude,
    normalize_env_dict,
    normalize_forward_env_names,
    resolve_passthrough_env,
)


class TestNormalizeForwardEnvNames:
    def test_dedupes_and_preserves_order(self):
        assert normalize_forward_env_names(
            ["B", "A", "B"], config_key="k8s_forward_env"
        ) == ["B", "A"]

    def test_strips_whitespace(self):
        assert normalize_forward_env_names(
            ["  TOKEN  "], config_key="k8s_forward_env"
        ) == ["TOKEN"]

    @pytest.mark.parametrize("bad", [
        "1LEADING_DIGIT",
        "HAS-DASH",
        "HAS SPACE",
        "HAS=EQUALS",
        "",
        None,
        42,
    ])
    def test_rejects_invalid_names(self, bad):
        """Anything that is not a shell-legal name would break the prelude."""
        assert normalize_forward_env_names([bad], config_key="k8s_forward_env") == []

    def test_none_is_empty(self):
        assert normalize_forward_env_names(None, config_key="k8s_forward_env") == []

    def test_a_bare_string_is_rejected_not_iterated(self):
        """The dangerous config.yaml slip: every character is a legal name.

        Without the list guard, `k8s_forward_env: "GH_TOKEN"` forwards $G, $H,
        $_, $T, $O, $K, $E, $N rather than being rejected.
        """
        assert normalize_forward_env_names(
            "GH_TOKEN", config_key="k8s_forward_env"
        ) == []


class TestNormalizeEnvDict:
    def test_coerces_yaml_scalars(self):
        """Unquoted YAML values arrive as int/bool, not str."""
        result = normalize_env_dict(
            {"PORT": 8080, "DEBUG": True, "RATIO": 1.5}, config_key="k8s_env"
        )
        assert result == {"PORT": "8080", "DEBUG": "True", "RATIO": "1.5"}

    def test_drops_structured_values(self):
        assert normalize_env_dict({"A": ["x"], "B": "ok"}, config_key="k8s_env") == {
            "B": "ok"
        }

    def test_drops_invalid_keys(self):
        assert normalize_env_dict({"BAD-KEY": "x"}, config_key="k8s_env") == {}

    def test_non_dict_is_empty(self):
        assert normalize_env_dict("NOT_A_DICT", config_key="k8s_env") == {}


class TestExportPrelude:
    def test_quotes_values(self):
        prelude = export_prelude({"MSG": "hello world; rm -rf /"})
        assert prelude == "export MSG='hello world; rm -rf /'\n"

    def test_quotes_embedded_single_quotes(self):
        prelude = export_prelude({"MSG": "it's"})
        # Round-trip through a shell to prove the quoting holds.
        import subprocess
        out = subprocess.run(
            ["bash", "-c", prelude + "printf %s \"$MSG\""],
            capture_output=True, text=True,
        )
        assert out.stdout == "it's"

    def test_unset_comes_before_export(self):
        """A name can be both stale in the snapshot and set in this scope."""
        prelude = export_prelude({"A": "1"}, {"A", "B"})
        assert prelude.index("unset") < prelude.index("export")
        assert prelude.endswith("export A=1\n")

    def test_every_statement_is_newline_terminated(self):
        """A command opening with a comment would otherwise eat the exports."""
        prelude = export_prelude({"A": "1", "B": "2"})
        # shlex.quote leaves shell-safe values bare; that is fine and expected.
        assert prelude == "export A=1\nexport B=2\n"

    def test_empty_is_empty(self):
        assert export_prelude({}, ()) == ""


class TestResolvePassthroughEnv:
    def test_reads_explicit_names_from_the_host(self, monkeypatch):
        monkeypatch.setenv("HERMES_TEST_FORWARDED", "value-1")
        env, _ = resolve_passthrough_env(["HERMES_TEST_FORWARDED"])
        assert env["HERMES_TEST_FORWARDED"] == "value-1"

    def test_unset_host_var_is_omitted(self, monkeypatch):
        monkeypatch.delenv("HERMES_TEST_ABSENT", raising=False)
        env, _ = resolve_passthrough_env(["HERMES_TEST_ABSENT"])
        assert "HERMES_TEST_ABSENT" not in env

    def test_explicit_opt_in_beats_the_provider_blocklist(self, monkeypatch):
        """Operator config is trusted where skill-registered passthrough is not.

        The blocklist exists to stop a malicious skill tunnelling a Hermes
        credential out (GHSA-rhgp-j443-p4rf); it is not meant to override an
        operator who explicitly listed the name in config.yaml.
        """
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST

        if not _HERMES_PROVIDER_ENV_BLOCKLIST:
            pytest.skip("no provider credentials registered in this install")
        name = sorted(_HERMES_PROVIDER_ENV_BLOCKLIST)[0]
        monkeypatch.setenv(name, "operator-opted-in")

        env, _ = resolve_passthrough_env([name])

        assert env[name] == "operator-opted-in"


class TestDockerParity:
    """Pin this module against the implementation it was lifted from.

    ``DockerEnvironment._resolve_passthrough_env`` reads nothing but
    ``self._forward_env``, so it can be called unbound against a stub — no
    Docker daemon, no container, and no import-time probe.
    """

    @staticmethod
    def _docker_resolve(forward_env):
        from tools.environments.docker import DockerEnvironment

        return DockerEnvironment._resolve_passthrough_env(
            SimpleNamespace(_forward_env=forward_env)
        )

    @pytest.mark.parametrize("forward_env", [
        [],
        ["HERMES_TEST_PARITY"],
        ["HERMES_TEST_PARITY", "HERMES_TEST_ABSENT"],
    ])
    def test_same_resolution(self, forward_env, monkeypatch):
        monkeypatch.setenv("HERMES_TEST_PARITY", "shared")
        monkeypatch.delenv("HERMES_TEST_ABSENT", raising=False)

        assert resolve_passthrough_env(forward_env) == self._docker_resolve(
            forward_env
        )

    def test_same_resolution_for_a_blocklisted_name(self, monkeypatch):
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST

        if not _HERMES_PROVIDER_ENV_BLOCKLIST:
            pytest.skip("no provider credentials registered in this install")
        name = sorted(_HERMES_PROVIDER_ENV_BLOCKLIST)[0]
        monkeypatch.setenv(name, "secret")

        assert resolve_passthrough_env([name]) == self._docker_resolve([name])
