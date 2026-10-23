"""Prepare files and compose the existing core, terminal and direct transport."""
import asyncio
from contextlib import AsyncExitStack, contextmanager
import json
import re
import secrets
import shutil
import tomllib


def read_settings(config):
    file = config.resolve()
    root = file.parent.parent
    settings = tomllib.loads(file.read_text(encoding="utf-8-sig"))["bridge"]
    state = (root / settings["state_root"]).resolve()
    if not state.is_relative_to(root) or state == root:
        raise ValueError("记忆库必须位于本项目目录内。")
    return root, settings, state


def wechat_file(args):
    return args.wechat_config or args.config.resolve().parent / "wechatagent.local.toml"


def prepare(args):
    file = args.config.resolve()
    root = file.parent.parent
    for target, template in ((file, root / "config/bridge.example.toml"),
                             (wechat_file(args), root / "config/wechatagent.example.toml")):
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, target)
    _, settings, _ = read_settings(file)
    token = root / settings["token_file"]
    token.parent.mkdir(parents=True, exist_ok=True)
    if not token.exists():
        with token.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    template = root / "config/harness/executor.example.json"
    target = template.with_name("executor.local.json")
    if template.exists() and not target.exists():
        shutil.copyfile(template, target)
    print("准备完成：仅补齐缺失的本地模板与令牌，已有文件保留。")


def check(args):
    from myatri.app import load_config
    from .server import create_app, service_lock  # noqa: F401
    from .wechat import WeChatRunner  # noqa: F401
    root, settings, state = read_settings(args.config)
    load_config(root / settings["engine_config"])
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("管理API仅支持127.0.0.1。")
    if settings.get("mode", "offline") not in {"offline", "live"}:
        raise ValueError("mode必须为offline或live。")
    if not (root / settings["persona_file"]).is_file():
        raise ValueError("角色卡不存在。")
    if len((root / settings["token_file"]).read_text(encoding="ascii").strip()) < 32:
        raise ValueError("管理令牌长度不足。")
    tomllib.loads(wechat_file(args).read_text(encoding="utf-8-sig"))["wechatagent"]
    print("通过：核心/传输入口导入、bridge/engine/wechat TOML、角色卡、令牌、库路径。")
    print(f"库路径：{state}\n未连接微信、未检查端口/凭据/外部运行区、未调用模型。")


@contextmanager
def locked(root):
    from .server import service_lock
    lock = service_lock(root / "var/service.lock")
    try:
        lock.__enter__()
    except OSError:
        raise RuntimeError("群聊或另一个私聊正在使用同一份库，请先正常退出。") from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def engine_for(root, settings, mode):
    from myatri.app import load_config
    settings["mode"] = mode
    engine = load_config(root / settings["engine_config"])
    engine["chat"]["allow_paid"] = mode == "live"
    return engine


async def chat(args):
    from .local_chat import chat as terminal_chat, existing_channels, local_message
    root, settings, state = read_settings(args.config)
    with locked(root):
        channels = existing_channels(state, settings)
        if args.scope:
            channels = [row for row in channels if row[0] == args.scope]
        if not channels:
            raise ValueError("没有找到已有的授权群会话库；本入口只延续已有库。")
        if len(channels) > 1:
            for number, (scope, route) in enumerate(channels, 1):
                print(f"{number}. {route.group_id} ({scope})")
            selected = int(await asyncio.to_thread(input, "编号 > ")) - 1
            if not 0 <= selected < len(channels):
                raise ValueError("会话库选择无效。")
            channels = [channels[selected]]
        route = channels[0][1]
        local_message(route, settings, "identity check")
        await terminal_chat(root, state, settings, engine_for(root, settings, args.mode), route)


def replace_setting(text, key, value):
    result, count = re.subn(rf"(?m)^{re.escape(key)}\s*=.*$",
                           lambda _: key + " = " + json.dumps(value, ensure_ascii=False), text, count=1)
    if count != 1:
        raise ValueError(f"配置中缺少唯一{key}字段。")
    tomllib.loads(result)
    return result


async def bind(args):
    from .wechat_client import NativeClient, Settings
    root, _, _ = read_settings(args.config)
    with locked(root):
        file = wechat_file(args)
        client = NativeClient(Settings.load(file))
        try:
            await client.bind()
            bridge_text = args.config.read_text(encoding="utf-8-sig")
            settings_text = file.read_text(encoding="utf-8-sig")
            groups = tomllib.loads(bridge_text)["bridge"].get("allowed_groups", [])
            route = "xiaowennuan-wechat|" + client.group_id
            bridge_text = replace_setting(bridge_text, "allowed_groups", list(dict.fromkeys([*groups, route])))
            settings_text = replace_setting(settings_text, "group_id", client.group_id)
            settings_text = replace_setting(settings_text, "bot_id", client.bot_id)
            args.config.write_text(bridge_text, encoding="utf-8")
            file.write_text(settings_text, encoding="utf-8")
        finally:
            await client.close()
    print("认证绑定完成：已保存真实群/机器人ID和bridge白名单。")


async def wechat(args):
    from aiohttp import web
    from .bridge import Bridge
    from .server import create_app
    from .wechat import WeChatRunner
    from .wechat_client import NativeClient, Settings
    root, settings, state = read_settings(args.config)
    if args.ui and args.config.resolve() != root / "config/bridge.local.toml":
        raise ValueError("现有监视窗口读取config/bridge.local.toml；自定义配置请不带--ui运行。")
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("管理API仅支持127.0.0.1。")
    native_settings = Settings.load(wechat_file(args))
    with locked(root):
        client = NativeClient(native_settings)
        try:
            # Authenticate before constructing/starting a Bridge with persisted jobs.
            await client.bind()
            route = "xiaowennuan-wechat|" + client.group_id
            if route not in settings.get("allowed_groups", []):
                raise ValueError("认证群不在bridge白名单中，请先运行bind。")
        except BaseException:
            await client.close()
            raise
        bridge = Bridge(state, settings, engine_for(root, settings, args.mode), root / settings["persona_file"])
        runner = WeChatRunner(bridge, client, observe_only=args.observe_only)
        async with AsyncExitStack() as stack:
            # Close the transport before the app's existing Bridge cleanup hook.
            api = web.AppRunner(create_app(bridge, (root / settings["token_file"]).read_text(encoding="ascii").strip()))
            stack.push_async_callback(api.cleanup)
            await api.setup()
            stack.push_async_callback(runner.close)
            await web.TCPSite(api, "127.0.0.1", settings.get("port", 8766)).start()
            if args.ui:
                from .monitor import create_monitor_app
                from .context_viewer import create_app as context_app
                for app, port in ((create_monitor_app(root), 8767), (context_app(root), 8768)):
                    ui = web.AppRunner(app)
                    stack.push_async_callback(ui.cleanup)
                    await ui.setup()
                    await web.TCPSite(ui, "127.0.0.1", port).start()
                print("监视：http://127.0.0.1:8767/  上下文：http://127.0.0.1:8768/")
            print(f"wechat · {args.mode} · Ctrl+C正常退出")
            await runner.run()
