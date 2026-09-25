"""Profile-scoped environment interpolation in routed terminal configuration."""

import json


def test_routed_docker_config_expands_home_and_profile_secrets_without_cross_profile_leaks(
    tmp_path, monkeypatch
):
    import gateway.run as gateway
    from agent import secret_scope
    from tools.terminal_scope import terminal_env

    home_a = tmp_path / "profiles" / "a"
    home_b = tmp_path / "profiles" / "b"
    home_a.mkdir(parents=True)
    home_b.mkdir(parents=True)
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()

    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setenv("AGENT_VAULT_A_ONLY", "launch-profile-secret")
    monkeypatch.setenv("AGENT_VAULT_PROXY", "launch-profile-proxy")
    monkeypatch.setenv("AGENT_VAULT_TOKEN", "launch-profile-token")

    (home_a / ".env").write_text(
        "AGENT_VAULT_A_ONLY=profile-a-only\n"
        "AGENT_VAULT_PROXY=profile-a-proxy\n"
        "AGENT_VAULT_TOKEN=profile-a-token\n",
        encoding="utf-8",
    )
    (home_b / ".env").write_text(
        "AGENT_VAULT_PROXY=profile-b-proxy\n"
        "AGENT_VAULT_TOKEN=profile-b-token\n",
        encoding="utf-8",
    )
    (home_a / "config.yaml").write_text(
        'terminal:\n'
        '  backend: docker\n'
        '  docker_volumes:\n'
        '    - "${HOME}/a:/home"\n'
        '    - "${AGENT_VAULT_PROXY}/a:/proxy"\n'
        '  docker_env:\n'
        '    AGENT_VAULT_TOKEN: "${AGENT_VAULT_TOKEN}"\n',
        encoding="utf-8",
    )
    (home_b / "config.yaml").write_text(
        'terminal:\n'
        '  backend: docker\n'
        '  docker_volumes:\n'
        '    - "${HOME}/b:/home"\n'
        '    - "${AGENT_VAULT_PROXY}/b:/proxy"\n'
        '  docker_env:\n'
        '    AGENT_VAULT_TOKEN: "${AGENT_VAULT_TOKEN}"\n'
        '    A_ONLY: "${AGENT_VAULT_A_ONLY}"\n',
        encoding="utf-8",
    )

    was_multiplex_active = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        for profile_home, suffix, proxy, token in (
            (home_a, "a", "profile-a-proxy", "profile-a-token"),
            (home_b, "b", "profile-b-proxy", "profile-b-token"),
            (home_a, "a", "profile-a-proxy", "profile-a-token"),
        ):
            with gateway._profile_runtime_scope(profile_home):
                volumes = json.loads(terminal_env("TERMINAL_DOCKER_VOLUMES"))
                docker_env = json.loads(terminal_env("TERMINAL_DOCKER_ENV"))
                assert volumes == [
                    f"{host_home}/{suffix}:/home",
                    f"{proxy}/{suffix}:/proxy",
                ]
                assert docker_env["AGENT_VAULT_TOKEN"] == token
                if profile_home == home_b:
                    assert docker_env["A_ONLY"] == "${AGENT_VAULT_A_ONLY}"
                else:
                    assert "A_ONLY" not in docker_env
    finally:
        secret_scope.set_multiplex_active(was_multiplex_active)
