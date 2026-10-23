"""Mutually exclusive terminal entry using the existing group conversation library."""
import argparse
import asyncio
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tomllib
import uuid

from myatri.app import load_config
from myatri.providers import ProviderError
from .bridge import Bridge, Incoming
from .server import service_lock


def existing_channels(state_root, settings):
    path = Path(state_root) / 'deliveries.sqlite3'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        routes = db.execute('SELECT scope,payload FROM group_routes ORDER BY scope').fetchall()
    channels = []
    for scope, payload in routes:
        route = Incoming.model_validate(json.loads(payload))
        if route.route in settings.get('allowed_groups', []):
            channels.append((scope, route))
    return channels


def local_message(route, settings, text):
    if not settings.get('owner_id') or settings['owner_id'] == route.bot_id:
        raise ValueError('本机私聊需要配置独立、固定的菅蘧owner_id。')
    return Incoming(platform_id=route.platform_id, bot_id=route.bot_id, group_id=route.group_id,
        sender_id=settings['owner_id'], sender_name=settings.get('owner_name') or '菅蘧',
        message_id='cli_' + uuid.uuid4().hex, sent_at=datetime.now(timezone.utc).isoformat(),
        text=text, mentioned=True, transport='local_cli')


async def reply(bridge, message, emit=print):
    result = await bridge.receive(message)
    if result['status'] == 'waiting_for_computer':
        emit('系统 > Harness执行中，结果返回后由小温暖回复。')
    while result['status'] == 'waiting_for_computer':
        await asyncio.sleep(.25)
        with closing(sqlite3.connect(bridge.path)) as db:
            result = bridge._delivery_result(db, message.key)
            report = db.execute('SELECT state FROM computer_reports WHERE delivery_id=?', (message.key,)).fetchone()
        if report and report[0] == 'failed':
            raise RuntimeError('任务执行记录已保存，但小温暖生成汇报失败；可用/pc状态查询。')
    if result['status'] == 'sent':
        emit('系统 > 本条回复已显示，不重复输出。')
        return
    if result['status'] != 'prepared':
        raise RuntimeError('本条回复尚未确认，请检查本机任务和投递状态。')
    if not (await bridge.claim(message.key))['claimed']:
        raise RuntimeError('本条回复已被领取，不重复输出。')
    try:
        for part in result['reply_parts']:
            emit('小温暖 > ' + part)
    except BaseException:
        await bridge.acknowledge(message.key, 'unknown')
        raise
    await bridge.acknowledge(message.key, 'sent')


async def chat(root, state_root, settings, engine, route):
    # Local mode has no WeChat sender; do not prepare unsolicited group speech.
    settings = deepcopy(settings)
    settings.setdefault('proactive', {})['enabled'] = False
    bridge = Bridge(state_root, settings, engine, root / settings['persona_file'])
    previous = None
    print(f"小温暖本机私聊 · {settings['mode']}模式\n共享库：{state_root / route.scope}\n"
          '命令：/exit 退出；/retry 重试上条未完成消息。电脑任务仍用自然语言或/pc。')
    try:
        while True:
            try:
                text = (await asyncio.to_thread(input, '\n你 > ')).strip()
            except EOFError:
                break
            if text in {'/exit', '/quit'}:
                break
            if not text:
                continue
            if text == '/retry':
                if previous is None:
                    print('系统 > 没有可重试的消息。')
                    continue
                message = previous
            else:
                message = previous = local_message(route, settings, text)
            try:
                await reply(bridge, message)
            except (ProviderError, RuntimeError, ValueError) as error:
                print(f'系统 > {error}\n原消息保留；/retry复用原消息编号，不重新提交已存在的任务。')
    finally:
        await bridge.close()


def main():
    parser = argparse.ArgumentParser(description='同库命令行私聊；不连接微信，不另建记忆或上下文库。')
    parser.add_argument('--config', default='config/bridge.local.toml')
    parser.add_argument('--scope', help='有多个群库时，指定要延续的32位scope')
    parser.add_argument('--offline', action='store_true', help='仅使用离线测试回复')
    args = parser.parse_args()
    file = Path(args.config).resolve()
    root = file.parent.parent
    settings = tomllib.loads(file.read_text(encoding='utf-8'))['bridge']
    state_root = (root / settings['state_root']).resolve()
    if not state_root.is_relative_to(root) or state_root == root:
        raise SystemExit('记忆库必须位于本项目目录内。')
    with ExitStack() as stack:
        try:
            stack.enter_context(service_lock(root / 'var/service.lock'))
        except OSError:
            raise SystemExit('群聊服务或另一个私聊窗口正在使用同一份库。请先正常退出，再切换模式。') from None
        channels = existing_channels(state_root, settings)
        if args.scope:
            channels = [row for row in channels if row[0] == args.scope]
        if not channels:
            raise SystemExit('没有找到已有的授权群会话库；本入口只延续已有库，不创建新的私聊库。')
        if len(channels) > 1:
            print('选择要接着聊的现有会话库：')
            for number, (scope, route) in enumerate(channels, 1):
                print(f'{number}. {route.group_id} ({scope})')
            try:
                selected = int(input('编号 > ')) - 1
                if not 0 <= selected < len(channels):
                    raise ValueError()
            except (ValueError, EOFError):
                raise SystemExit('会话库选择无效。') from None
            channels = [channels[selected]]
        route = channels[0][1]
        # Validate identity before loading the engine or making a model request.
        local_message(route, settings, 'identity check')
        if args.offline:
            settings['mode'] = 'offline'
        engine = load_config(root / settings['engine_config'])
        engine['chat']['allow_paid'] = settings['mode'] == 'live'
        try:
            asyncio.run(chat(root, state_root, settings, engine, route))
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
