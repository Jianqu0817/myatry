"""Selected preserved-regression cases from MYATRI-AstrBot 4428069; fixtures only."""
from contextlib import closing

import ast

import asyncio

import hashlib

import json

from pathlib import Path

import sqlite3

from types import SimpleNamespace

import time

import logging

import pytest

import httpx

from myatri.app import load_config

from myatri.providers import FixtureProvider

from myatry.bridge import Bridge, Incoming

from myatry.server import pending_deliveries

PROJECT = Path(__file__).resolve().parents[1]

def setup(tmp_path, replies=None):
    provider = FixtureProvider(replies or [{'content': '查到了，可用内存8.90GB\n查询发生在12:07，当前数值可能有变化'}])
    settings = {'project_root': str(tmp_path), 'mode': 'live', 'allowed_groups': ['fixture|room'],
        'owner_id': 'owner', 'owner_name': '菅蘧', 'random_reply_probability': 0,
        'quality_guard_enabled': False, 'computer': {'enabled': True, 'agent_enabled': True,
            'permission_profile': 'full', 'owner_id': 'owner', 'node_path': 'node.exe', 'runtime_root': 'unused'}}
    bridge = Bridge(tmp_path / 'groups', settings, load_config(PROJECT / 'config/engine.toml'),
                    PROJECT / 'config/persona/persona.md', provider_factory=lambda _: provider)
    message = Incoming(platform_id='fixture', bot_id='bot', group_id='room', sender_id='owner',
        message_id='pc-original', text='/pc 任务 查看内存', mentioned=True,
        sent_at='2026-10-22T12:07:00+08:00')
    bridge.routes[message.scope] = message.model_dump()
    return bridge, provider, message

def task(bridge, message, *, terminal='completed', initial='waiting_for_computer', suffix='', envelope_extra=None):
    source = message.model_copy(update={'message_id': message.message_id + suffix})
    task_id = hashlib.sha256(json.dumps([source.platform_id, source.bot_id, source.group_id, source.message_id],
                                       ensure_ascii=False).encode()).hexdigest()
    bridge.computer_agent.store.create(task_id, source.sender_id, source.scope, '查看内存', {
        'report_to_chat': False, 'reply_mode': 'conversation', 'channel_source': source.model_dump()}, 'hash')
    bridge.computer_agent.store.state(task_id, terminal, '12:07可用内存8.90GB；查询已验证。')
    envelope = json.dumps(source.model_dump(exclude={'mentioned'}) | (envelope_extra or {}))
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute('INSERT INTO deliveries(id,scope,fingerprint,status,updated,envelope) VALUES(?,?,?,?,?,?)',
            (source.key, source.scope, 'fixture', initial, 1, envelope))
    app = bridge._application(source)
    app.store.append_message('user', envelope, message_id=source.key, channel_source=source.model_dump())
    app.queue.save_protocol(source.key, {'status': 'active', 'snapshot': app.store.snapshot(), 'steps': 1,
        'messages': [{'role': 'user', 'content': envelope},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'delegate', 'type': 'function',
                'function': {'name': 'computer_control', 'arguments': '{"instructions":"查询内存并核验"}'}}]},
            {'role': 'tool', 'tool_call_id': 'delegate', 'content': json.dumps({'task_id': task_id, 'state': 'queued'})}],
        'computer_wait': {'call_id': 'delegate', 'arguments': {'instructions': '查询内存并核验'}}})
    bridge.computer_reports.wait(source, task_id)
    return task_id, source

async def test_main_persona_receives_report_and_prepares_wechat_without_another_human_turn(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task_id, source = task(bridge, message)
    try:
        await bridge.computer_reports.tick()
        assert len(provider.requests) == 1 and provider.requests[0].purpose == 'conversation'
        receipt = next(m for m in provider.requests[0].messages if m['role'] == 'tool')
        assert receipt['tool_call_id'] == 'delegate'
        assert '8.90GB' in receipt['content']
        assert json.loads(receipt['content'])['state'] == 'completed'
        protocol = bridge.apps[message.scope].queue.get_protocol(message.key)
        assert protocol['status'] == 'completed'
        assert any(m['role'] == 'tool' and '8.90GB' in m['content'] for m in protocol['messages'])
        assert len(bridge.apps[message.scope].store.list_records('raw_message')) == 2
        pending = pending_deliveries(bridge.path, message.scope)
        assert len(pending) == 1
        delivery = pending[0]
        assert len(delivery['reply_parts']) == 2 and delivery['id'] == message.key
        assert (await bridge.claim(delivery['id']))['claimed'] is True
        await bridge.acknowledge(delivery['id'], 'sent', 0)
        await bridge.acknowledge(delivery['id'], 'sent', 1)
        assert len(bridge.short_memory.recent_assistant(message.scope)) == 2
        await bridge.computer_reports.tick()
        assert len(provider.requests) == 1 and pending_deliveries(bridge.path, message.scope) == []
    finally:
        await bridge.close()

@pytest.mark.parametrize('terminal', ['completed', 'blocked', 'unknown', 'paused', 'cancelled'])
async def test_waiting_turn_survives_restart_and_returns_actual_state(tmp_path, terminal):
    bridge, provider, message = setup(tmp_path)
    task_id, _ = task(bridge, message, terminal=terminal)
    settings, engine = bridge.config, bridge.engine_config
    await bridge.close()
    restored = Bridge(tmp_path / 'groups', settings, engine, PROJECT / 'config/persona/persona.md',
                      provider_factory=lambda _: provider)
    try:
        await restored.computer_reports.tick()
        receipt = next(m for m in provider.requests[-1].messages if m['role'] == 'tool')
        assert json.loads(receipt['content'])['state'] == terminal
        assert json.loads(receipt['content'])['report']['task_id'] == task_id
        assert len(restored.computer_agent.store.list()) == 1
        assert pending_deliveries(restored.path, message.scope)[0]['id'] == message.key
    finally:
        await restored.close()
