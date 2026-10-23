"""Offline direct transport checks; no real reader, model or WeChat calls."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatry.bridge import Bridge
from myatry.wechat import WeChatRunner
from myatry.wechat_client import NativeClient, NotReady, Settings

pytestmark = pytest.mark.asyncio
PROJECT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, fail_part=None):
        self.bot_id = 'bot'
        self.group_id = 'room@chatroom'
        self.settings = SimpleNamespace(bot_nickname='小温暖', bot_id=self.bot_id, group_id=self.group_id)
        self.sent = []
        self.fail_part = fail_part
        self.closed = False

    async def send(self, text):
        self.sent.append(text)
        if len(self.sent) == self.fail_part:
            raise NotReady('SEND_RECEIPT_UNKNOWN')

    async def close(self):
        self.closed = True


def event(text='你好', **updates):
    return dict(event='message.new', type=1, sessionType='group',
                sessionId='room@chatroom', isSend=False, talkerId='owner',
                rawid='m1', timestamp=int(time.time()), content=text, sourceName='测试成员') | updates


def make_bridge(tmp_path, *, computer=False):
    provider = FixtureProvider([{'content': '第一段回复。\n第二段回复。'}] * 4)
    config = {'project_root': str(tmp_path), 'mode': 'live' if computer else 'offline',
              'allowed_groups': ['xiaowennuan-wechat|room@chatroom'],
              'owner_id': 'owner', 'random_reply_probability': 0,
              'quality_guard_enabled': False, 'proactive': {'enabled': False}}
    if computer:
        config['computer'] = {'enabled': True, 'agent_enabled': True,
            'permission_profile': 'full', 'owner_id': 'owner',
            'node_path': 'node.exe', 'runtime_root': 'unused'}
    bridge = Bridge(tmp_path / 'groups', config, load_config(PROJECT / 'config/engine.toml'),
                    PROJECT / 'config/persona/persona.md', provider_factory=lambda _: provider)
    return bridge, provider


async def test_mapping_filters_quote_and_original_scope():
    client = FakeClient()
    bridge = SimpleNamespace(config={'allowed_groups': ['xiaowennuan-wechat|room@chatroom']})
    runner = WeChatRunner(bridge, client)
    ordinary = runner.message(event())
    assert ordinary.text == '你好' and not ordinary.mentioned and ordinary.transport == 'astrbot'
    assert runner.message(event('/warm 你好')).mentioned
    assert runner.message(event('/warm 你好')).text == '你好'
    at = runner.message(event('@小温暖\u2005你好'))
    assert at.mentioned and at.mentioned_ids == ['bot']
    reference = dict(conversationId=client.group_id, kind='1', text='原话', senderId='friend',
                     truncated=False, unavailable=False, messageId='91')
    quoted = runner.message(event('回复', type=49, quoteReply=dict(version=1, truncated=False,
                                   reference=reference, reply='回复')))
    assert quoted.message_type == 'quote' and quoted.quoted_message.text == '原话'
    for change in ({'sessionId': 'other'}, {'talkerId': 'bot'}, {'isSend': True},
                   {'timestamp': runner.started_at - 1}, {'type': 3}, {'rawid': ''}):
        assert runner.message(event(**change)) is None
    bridge.config['allowed_groups'] = []
    assert runner.message(event()) is None


@pytest.mark.parametrize('fail_part', [None, 2])
async def test_real_bridge_wake_listening_multipart_dedup_and_unknown(tmp_path, fail_part):
    bridge, provider = make_bridge(tmp_path)
    client = FakeClient(fail_part)
    runner = WeChatRunner(bridge, client)
    try:
        ordinary = event('普通群消息', rawid='ordinary')
        await runner.handle(ordinary)
        message = runner.message(ordinary)
        assert bridge.short_memory.recent(message.scope)[0]['text'] == '普通群消息'
        assert not provider.requests
        wake = event('/warm 你好', rawid='wake')
        await runner.handle(wake)
        await runner.handle(wake)
        assert client.sent == ['第一段回复。', '第二段回复。']
        with sqlite3.connect(bridge.path) as db:
            state, count = db.execute('SELECT status,sent_parts FROM deliveries WHERE id=?',
                                     (runner.message(wake).key,)).fetchone()
        assert (state, count) == (('unknown', 1) if fail_part else ('sent', 2))
        assert not (await bridge.claim(runner.message(wake).key))['claimed']
        assert len(provider.requests) == 1
        await runner.handle(event('小温暖在吗', rawid='name-wake'))
        assert len(provider.requests) == 2
    finally:
        await runner.close()
        await bridge.close()


async def test_harness_final_reply_polled_without_proactive_or_another_message(tmp_path):
    bridge, provider = make_bridge(tmp_path, computer=True)
    runner = WeChatRunner(bridge, FakeClient())
    source = runner.message(event('/pc 任务 查看内存'))
    runner.scope = source.scope
    bridge.routes[source.scope] = source.model_dump()
    task_id = hashlib.sha256(json.dumps([source.platform_id, source.bot_id, source.group_id,
                                        source.message_id], ensure_ascii=False).encode()).hexdigest()
    bridge.computer_agent.store.create(task_id, source.sender_id, source.scope, '查看内存',
        {'report_to_chat': False, 'reply_mode': 'conversation', 'channel_source': source.model_dump()}, 'hash')
    bridge.computer_agent.store.state(task_id, 'completed', '已核验可用内存8.90GB')
    envelope = json.dumps(source.model_dump(exclude={'mentioned'}))
    with sqlite3.connect(bridge.path) as db:
        db.execute('INSERT INTO deliveries(id,scope,fingerprint,status,updated,envelope) VALUES(?,?,?,?,?,?)',
                   (source.key, source.scope, 'fixture', 'waiting_for_computer', time.time(), envelope))
    app = bridge._application(source)
    app.store.append_message('user', envelope, message_id=source.key, channel_source=source.model_dump())
    app.queue.save_protocol(source.key, {'status': 'active', 'snapshot': app.store.snapshot(), 'steps': 1,
        'messages': [{'role': 'user', 'content': envelope},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'delegate', 'type': 'function',
                'function': {'name': 'computer_control', 'arguments': '{"instructions":"查询内存并核验"}'}}]},
            {'role': 'tool', 'tool_call_id': 'delegate', 'content': json.dumps({'task_id': task_id, 'state': 'queued'})}],
        'computer_wait': {'call_id': 'delegate', 'arguments': {'instructions': '查询内存并核验'}}})
    bridge.computer_reports.wait(source, task_id)
    try:
        await runner.poll()
        assert runner.client.sent == []
        await bridge.computer_reports.tick()
        receipt = next(m for m in provider.requests[0].messages if m['role'] == 'tool')
        assert receipt['tool_call_id'] == 'delegate' and '8.90GB' in receipt['content']
        await runner.poll()
        await runner.poll()
        assert runner.client.sent == ['第一段回复。', '第二段回复。']
        assert bridge.computer_reports.status(task_id)['delivery_state'] == 'sent'
    finally:
        await runner.close()
        await bridge.close()


async def test_native_auth_binding_receipt_and_unknown_single_submission():
    for receipt in (True, False):
        sent = []
        def handler(request):
            if request.url.path == '/api/v1/reader/status':
                assert request.headers['Authorization'] == 'Bearer ' + 'r' * 48
                body = dict(success=True, backend='weflow-cli-nt', accountDirectory='bot_db', ownWxid='bot')
            elif request.url.path == '/WeChatAgent/health':
                assert request.headers['Authorization'] == 'Bearer ' + 'a' * 64
                body = dict(integration='WeChatAgent-1', backend='wechat-hook', version='4.1.10.27', databaseAccounts=['bot_db'])
            elif request.url.path == '/api/v1/sessions':
                body = {'sessions': [{'username': 'room@chatroom', 'displayName': '测试群'}]}
            elif request.url.path == '/SendTextMsg':
                assert request.headers['X-WeChatAgent-Account'] == 'bot_db'
                sent.append(json.loads(request.content))
                body = {'ret': 0}
            elif request.url.path == '/api/v1/messages':
                body = {'messages': [dict(localId=7, localType=1, createTime=int(time.time()),
                    isSend=1, fullText='你好', serverId='123')] if sent and receipt else []}
            else:
                raise AssertionError(request.url.path)
            return httpx.Response(200, json=body)
        client = NativeClient(Settings('http://127.0.0.1:5031', 'r' * 48,
            'http://127.0.0.1:30001', 'a' * 64, '测试群', '小温暖'),
            transport=httpx.MockTransport(handler), receipt_timeout=0)
        try:
            await client.bind()
            if receipt:
                await client.send('你好')
            else:
                with pytest.raises(NotReady, match='SEND_RECEIPT_UNKNOWN'):
                    await client.send('你好')
            assert sent == [{'wxidorgid': 'room@chatroom', 'msg': '你好'}]
        finally:
            await client.close()
