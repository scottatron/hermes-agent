"""Slack slash pre-ack decisions shared with the host's routed chat policy."""
from gateway.output_policy import suppress_slack_chat_ack
from plugins.platforms.slack.adapter_slash_payload import slash_thread_id


def channel_gated(adapter, channel_id):
    if adapter._is_ignored_channel(channel_id):
        return True
    allowed = adapter._slack_allowed_channels()
    return bool(allowed) and not str(channel_id).startswith('D') and channel_id not in allowed


async def observe_only(adapter, command):
    channel = command.get('channel_id', '')
    source = adapter.build_source(
        chat_id=channel, chat_type='dm' if str(channel).startswith('D') else 'group',
        user_id=command.get('user_id', ''), thread_id=slash_thread_id(command),
        scope_id=command.get('team_id') or None)
    return await suppress_slack_chat_ack(adapter, source)
