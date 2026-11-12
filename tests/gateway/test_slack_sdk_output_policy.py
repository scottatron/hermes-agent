"""Generated Slack SDK methods must use the protected host transport boundary."""
import pytest

sdk = pytest.importorskip('slack_sdk.web.async_client')

from gateway.output_policy import OutputPolicy
from plugins.platforms.slack.adapter_output_policy import SlackOutputDenied, install_slack_api_guard


class RecordingClient(sdk.AsyncWebClient):
    def __init__(self):
        super().__init__(token='xoxb-fixture')
        self.transported = []

    async def api_call(self, api_method, **kwargs):
        self.transported.append(api_method)
        return {'ok': True}


@pytest.mark.asyncio
async def test_generated_sdk_writes_and_uploads_are_denied_while_intake_reads_work():
    client = install_slack_api_guard(RecordingClient(), OutputPolicy('/protected', 'deny'))
    for call in (
        client.chat_postMessage(channel='C1', text='never sent'),
        client.chat_update(channel='C1', ts='1.0', text='never edited'),
        client.reactions_add(channel='C1', timestamp='1.0', name='eyes'),
        client.files_upload_v2(channel='C1', content='never uploaded'),
        client.assistant_threads_setStatus(channel_id='C1', thread_ts='1.0', status='never shown'),
    ):
        with pytest.raises(SlackOutputDenied):
            await call
    assert client.transported == []
    await client.auth_test()
    await client.conversations_history(channel='C1')
    await client.apps_connections_open(app_token='xapp-fixture')
    assert client.transported == ['auth.test', 'conversations.history', 'apps.connections.open']
