"""Local sessions keep their launch policy after multiplex identity resolution.

A secondary local session initially resolved its policy, but its first turn
inferred the primary transport and subsequent metadata lost cwd/source policy.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


@pytest.mark.parametrize('profile', ['default', 'alpha'])
def test_local_policy_survives_identity_resolution(tmp_path, monkeypatch, profile):
    from gateway.run import _profile_runtime_scope
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_identity import identity_of
    from gateway.session_local import LocalSessionAdapter
    from gateway.session_policy import build_policy, policy_for_source

    root, homes = _reserve_homes(tmp_path, monkeypatch, names=('alpha',))
    runner = _runner(root, homes)
    authorities = {}
    adapters = {}
    for name, home in homes:
        authority = SimpleNamespace(profile_id=str(home), sessions={})
        authorities[name] = authority
        adapters[name] = LocalSessionAdapter(authority)
    runner.adapters[Platform.LOCAL] = adapters['default']
    runner._profile_adapters['alpha'] = {Platform.LOCAL: adapters['alpha']}

    source = SessionSource(platform=Platform.LOCAL, chat_id='local-session', user_id='operator',
                           profile=None if profile == 'default' else profile)
    owner, adapter = authorities[profile], adapters[profile]
    owner.sessions[source.chat_id] = SimpleNamespace(source=source)
    workspace = tmp_path / profile / 'workspace'
    workspace.mkdir(parents=True)
    with _profile_runtime_scope(dict(homes)[profile], {}):
        policy = build_policy({'source': 'cli', 'cwd': str(workspace), 'toolsets': []}, {})
    adapter.policies[source.chat_id] = policy
    adapter.register_source(source)
    try:
        assert policy_for_source(runner, source) is policy

        # This is the identity seam called by the first gateway turn.
        runner._canonicalize(source)
        identity = identity_of(source)
        assert identity.transport_profile == profile
        assert identity.runtime_profile == profile
        assert runner._adapter_for_source(source) is adapter
        assert policy_for_source(runner, source) is policy
        assert policy_for_source(runner, source).cwd == str(workspace)

        # Adapter provenance never replaces the source-object ownership check.
        copied = replace(source)
        assert not adapter.authorize_source(copied)
        assert policy_for_source(runner, copied) is None
    finally:
        runner.session_store.close_all_db_handles()
