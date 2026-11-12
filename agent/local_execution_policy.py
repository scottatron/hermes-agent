"""Host-owned capability checks for local skill execution."""
from contextlib import contextmanager
from contextvars import ContextVar


_LOCAL_ONLY = ContextVar("local_only_execution", default=False)


@contextmanager
def local_only_scope(enabled: bool):
    token = _LOCAL_ONLY.set(bool(enabled))
    try:
        yield
    finally:
        _LOCAL_ONLY.reset(token)


def local_only_execution() -> bool:
    """Whether the current turn runs under a persisted local-only session policy."""
    from agent.safe_worker_policy import local_only_worker_enabled

    if local_only_worker_enabled():
        return True
    return _LOCAL_ONLY.get()


def local_only_tool_message(tool_name: str) -> str:
    """Stable refusal text returned when a local-only turn asks to run a tool."""
    return f"Tool '{tool_name}' is disabled for this local-only session."


def local_only_tool_refusal(tool_name: str) -> str:
    """JSON refusal returned from the generic registry dispatcher."""
    import json

    return json.dumps({"error": local_only_tool_message(tool_name)})
