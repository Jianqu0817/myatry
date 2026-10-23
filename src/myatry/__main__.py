"""Single project entry; runtime mode must be chosen explicitly."""
import argparse
import asyncio
from pathlib import Path


def parser():
    root = argparse.ArgumentParser(prog="python -m myatry")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check", "bind", "chat", "wechat", "versions"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config/bridge.local.toml"))
        if name in {"prepare", "check", "bind", "wechat", "versions"}:
            command.add_argument("--wechat-config", type=Path)
        if name in {"chat", "wechat"}:
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--offline", dest="mode", action="store_const", const="offline")
            mode.add_argument("--live", dest="mode", action="store_const", const="live")
        if name == "chat":
            command.add_argument("--scope")
        if name == "wechat":
            command.add_argument("--observe-only", action="store_true", help="接收观察，不提交回复")
            command.add_argument("--ui", action="store_true", help="同时提供8767监视与8768上下文窗口")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    from . import launch
    try:
        if args.command in {"prepare", "check", "versions"}:
            return getattr(launch, args.command)(args)
        asyncio.run(getattr(launch, args.command)(args))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
