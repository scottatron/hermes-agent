"""Memory providers receive the session's logical cwd in gateway/Desktop sessions (#144869).

``_memory_provider_init_kwargs`` forwarded ``cwd`` only from ``agent.session_cwd``, which the
TUI sets but gateway/Desktop sessions do not: they pin the turn cwd via ``set_session_cwd()``
(policy scope). Providers got no cwd there and fell back to empty/default scope. The kwargs
must fall back to the scoped session cwd (never ``os.getcwd()``); an explicit
``agent.session_cwd`` still wins.
"""

from types import SimpleNamespace

from agent.agent_init_memory_provider import _memory_provider_init_kwargs
from agent.runtime_cwd import clear_session_cwd, reset_session_cwd, set_session_cwd


def _agent(session_cwd=None):
    return SimpleNamespace(
        session_id="sid", _session_db=None, _emit_warning=lambda *_: None,
        _emit_status=lambda *_: None, _emit_startup_warning=lambda *_: None,
        session_cwd=session_cwd,
        _user_id="u", _user_id_alt=None, _user_name=None,
        _chat_id="c", _chat_name=None, _chat_type=None, _thread_id=None,
        _gateway_session_key=None,
    )


def test_scoped_session_cwd_reaches_memory_providers():
    token = set_session_cwd("/tmp/project-a")
    try:
        kwargs = _memory_provider_init_kwargs(_agent(), "telegram")
        assert kwargs["cwd"] == "/tmp/project-a"
    finally:
        clear_session_cwd()
        reset_session_cwd(token)


def test_explicit_session_cwd_wins_and_no_scope_never_guesses():
    token = set_session_cwd("/tmp/project-a")
    try:
        assert (
            _memory_provider_init_kwargs(_agent(session_cwd="/tmp/explicit"), "telegram")["cwd"]
            == "/tmp/explicit"
        )
    finally:
        clear_session_cwd()
        reset_session_cwd(token)
    # No pinned scope and no session_cwd: no cwd key at all (never os.getcwd()).
    assert "cwd" not in _memory_provider_init_kwargs(_agent(), "cli")
