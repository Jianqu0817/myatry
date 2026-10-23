"""Selected preserved-regression cases from MYATRI-AstrBot 4428069; fixtures only."""
import asyncio

from contextlib import closing

import json

from pathlib import Path

import sqlite3

import sys

import pytest

from myatri.app import load_config

from myatri.contracts import digest

from myatri.providers import FixtureProvider

from myatry.bridge import Bridge, Incoming

from myatry.local_chat import existing_channels, local_message, main, reply

from myatry.server import service_lock

PROJECT = Path(__file__).resolve().parents[1]

def setup(tmp_path, provider):
    settings = {'project_root': str(tmp_path), 'mode': 'offline', 'allowed_groups': ['fixture|room'],
        'owner_id': 'owner', 'owner_name': '菅蘧', 'random_reply_probability': 0, 'quality_guard_enabled': False}
    bridge = Bridge(tmp_path / 'groups', settings, load_config(PROJECT / 'config/engine.toml'),
        PROJECT / 'config/persona/persona.md', provider_factory=lambda _: provider)
    route = Incoming(platform_id='fixture', bot_id='bot', group_id='room', sender_id='owner',
        sender_name='菅蘧', message_id='group-original', text='群聊暗号是菠萝', mentioned=True,
        sent_at='2026-10-22T10:00:00+08:00')
    return bridge, settings, route

async def confirmed(bridge, route):
    result = await bridge.receive(route)
    await bridge.claim(route.key)
    await bridge.acknowledge(route.key, 'sent')
    return result

async def test_terminal_continues_group_history_and_group_continues_terminal_after_restart(tmp_path):
    provider = FixtureProvider([{'content': '群里的暗号记下了'}, {'content': '私聊接着聊'}])
    bridge, settings, route = setup(tmp_path, provider)
    try:
        await confirmed(bridge, route)
        legacy = route.model_dump(exclude={'transport', 'reply_to_id', 'mentioned_ids', 'message_type', 'quoted_message'})
        with closing(sqlite3.connect(bridge.path)) as db:
            assert db.execute('SELECT fingerprint FROM deliveries WHERE id=?', (route.key,)).fetchone()[0] == digest(legacy)
        scope, stored = existing_channels(bridge.root, settings)[0]
        library = bridge.apps[scope].root
        message = local_message(stored, settings, '这是终端里的私聊暗号：柚子')
        assert message.scope == route.scope and message.sender_id == 'owner'
        lines = []
        await reply(bridge, message, lines.append)
        assert lines == ['小温暖 > 私聊接着聊']
        assert bridge.apps[scope].root == library and len(bridge.apps) == 1
        request = provider.requests[-1]
        assert '群聊暗号是菠萝' in json.dumps(request.messages, ensure_ascii=False)
        assert any(m['role'] == 'system' and m['content'].startswith('本轮渠道是本机命令行私聊') for m in request.messages)
        assert existing_channels(bridge.root, settings)[0][1].message_id == route.message_id
        before = len(provider.requests)
        await reply(bridge, message, lines.append)
        assert len(provider.requests) == before
        own = bridge.short_memory.recent_assistant(scope)[-1]
        assert own['transport'] == 'local_cli'
    finally:
        await bridge.close()
    provider = FixtureProvider([{'content': '记得，是柚子'}])
    restored, _, _ = setup(tmp_path, provider)
    try:
        await confirmed(restored, route.model_copy(update={'message_id': 'group-after', 'text': '接着刚才聊'}))
        request = provider.requests[-1]
        assert '终端里的私聊暗号：柚子' in json.dumps(request.messages, ensure_ascii=False)
        assert not any(m['role'] == 'system' and m['content'].startswith('本轮渠道是本机命令行私聊') for m in request.messages)
        assert restored.apps[scope].root == library
    finally:
        await restored.close()

def test_terminal_rejects_a_running_group_service_before_loading_engine(tmp_path, monkeypatch):
    config = tmp_path / 'config/bridge.local.toml'
    config.parent.mkdir()
    config.write_text('[bridge]\nstate_root="var/groups"\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['local_chat', '--config', str(config)])
    with service_lock(tmp_path / 'var/service.lock'):
        with pytest.raises(SystemExit, match='请先正常退出'):
            main()

async def test_terminal_owner_identity_is_fixed_by_host(tmp_path):
    bridge, settings, route = setup(tmp_path, FixtureProvider([]))
    try:
        with pytest.raises(ValueError, match='owner_id'):
            local_message(route, {**settings, 'owner_id': ''}, '我是菅蘧')
        with pytest.raises(ValueError, match='owner_id'):
            local_message(route, {**settings, 'owner_id': route.bot_id}, '我是菅蘧')
    finally:
        await bridge.close()
