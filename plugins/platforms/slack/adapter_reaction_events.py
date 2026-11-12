"""Stable Slack reaction observations through the gateway's routed authorization seam."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def publish_reaction_event(adapter, event: dict, *, removed: bool) -> None:
    """Publish without fetching message history, starting a turn, or exposing raw SDK data."""
    handler = getattr(adapter, "_platform_event_handler", None)
    if handler is None:
        return
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "message":
        return
    channel = item.get("channel")
    message = item.get("ts")
    user = event.get("user")
    emoji = event.get("reaction")
    timestamp = event.get("event_ts")
    if not all(isinstance(value, str) and value and len(value) <= 256
               for value in (channel, message, user, emoji, timestamp)):
        return
    if user == getattr(adapter, "_bot_user_id", None):
        return
    team = event.get("team") or getattr(adapter, "_channel_team", {}).get(channel)
    clients = getattr(adapter, "_team_clients", {})
    if not team and len(clients) == 1:
        team = next(iter(clients))
    if not isinstance(team, str) or not team:
        return  # A multi-workspace observer must never guess the owning tenant.
    payload = {
        "chat_id": channel, "message_id": message, "thread_id": None,
        "user_id": user, "team_id": team, "event_ts": timestamp,
        "reaction": emoji, "action": "removed" if removed else "added",
    }
    try:
        source = adapter.build_source(
            chat_id=channel, chat_type="dm" if channel.startswith("D") else "group",
            user_id=user, scope_id=team, message_id=message,
        )
        await handler({"platform": "slack", "event_type": "reaction", "payload": payload}, source)
    except Exception:
        # Observation cannot disrupt Slack's update acknowledgement or its legacy hooks.
        logger.debug("Slack reaction observer dispatch failed", exc_info=True)
