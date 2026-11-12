"""Slack SDK boundary for host-managed output policy."""

from __future__ import annotations

from typing import Any, Callable

from gateway.output_policy import OutputPolicy, slack_output_denied


class SlackOutputDenied(PermissionError):
    """Raised before a protected Slack API write reaches the SDK transport."""


# Slack reads used by the adapter. Unknown API methods fail closed in a protected profile.
_READ_METHODS = frozenset({
    "apps.connections.open",
    "auth.teams.list",
    "auth.test",
    "bots.info",
    "conversations.history",
    "conversations.info",
    "conversations.replies",
    "files.info",
    "team.info",
    "users.info",
})


def install_slack_api_guard(client: Any, owner_policy: OutputPolicy | None = None) -> Any:
    """Guard the SDK instance method that every generated Web API method calls."""
    state = getattr(client, "_hermes_slack_output_guard", None)
    if isinstance(state, dict):
        previous = state.get("owner_policy")
        if previous is None or (previous.slack == "allow" and owner_policy is not None
                                and owner_policy.slack == "deny"):
            state["owner_policy"] = owner_policy
        return client

    original: Callable[..., Any] = client.api_call
    state = {"owner_policy": owner_policy}

    async def guarded_api_call(api_method: str, **kwargs: Any) -> Any:
        if api_method not in _READ_METHODS and slack_output_denied(state["owner_policy"]):
            raise SlackOutputDenied(
                f"Slack output is denied by the active profile policy ({api_method})")
        return await original(api_method, **kwargs)

    client.api_call = guarded_api_call
    client._hermes_slack_output_guard = state
    upload = getattr(client, "files_upload_v2", None)
    if callable(upload):
        async def guarded_upload(*args: Any, **kwargs: Any) -> Any:
            if slack_output_denied(state["owner_policy"]):
                raise SlackOutputDenied(
                    "Slack output is denied by the active profile policy (files_upload_v2)")
            return await upload(*args, **kwargs)

        client.files_upload_v2 = guarded_upload
    return client


def require_slack_output_allowed(method: str, owner_policy: OutputPolicy | None = None) -> None:
    """Guard Slack write paths that bypass the SDK and use raw HTTP or response URLs."""
    if slack_output_denied(owner_policy):
        raise SlackOutputDenied(f"Slack output is denied by the active profile policy ({method})")
