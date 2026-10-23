from argparse import Namespace
from pathlib import Path
import shutil
import tomllib

import pytest

from myatry.__main__ import parser
from myatry import launch


ROOT = Path(__file__).resolve().parents[1]


def prepared(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config",
                    ignore=shutil.ignore_patterns("*.local.toml", "*.local.json"))
    args = Namespace(config=tmp_path / "config/bridge.local.toml", wechat_config=None)
    launch.prepare(args)
    return args


def test_modes_are_explicit():
    with pytest.raises(SystemExit):
        parser().parse_args(["wechat"])
    assert parser().parse_args(["chat", "--offline"]).mode == "offline"
    assert parser().parse_args(["wechat", "--live"]).mode == "live"


def test_prepare_preserves_existing_files_and_check_is_offline(tmp_path, monkeypatch):
    args = prepared(tmp_path)
    files = [args.config, launch.wechat_file(args), tmp_path / "var/bridge-token.txt"]
    original = [file.read_bytes() for file in files]
    launch.prepare(args)
    assert [file.read_bytes() for file in files] == original
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("check attempted network"))
    launch.check(args)
    assert not (tmp_path / "var/groups").exists()


@pytest.mark.asyncio
async def test_chat_rejects_empty_library_before_loading_model(tmp_path):
    args = prepared(tmp_path)
    args.scope, args.mode = None, "offline"
    with pytest.raises(ValueError, match="已有的授权群会话库"):
        await launch.chat(args)


@pytest.mark.asyncio
async def test_bind_saves_authenticated_ids_only(tmp_path, monkeypatch):
    args = prepared(tmp_path)
    from myatry import wechat_client
    class Client:
        group_id, bot_id = "real@chatroom", "real_bot"
        def __init__(self, settings):
            pass
        async def bind(self):
            pass
        async def close(self):
            pass
    monkeypatch.setattr(wechat_client.Settings, "load", lambda _: object())
    monkeypatch.setattr(wechat_client, "NativeClient", Client)
    await launch.bind(args)
    bridge = tomllib.loads(args.config.read_text(encoding="utf-8"))["bridge"]
    wechat = tomllib.loads(launch.wechat_file(args).read_text(encoding="utf-8"))["wechatagent"]
    assert bridge["allowed_groups"] == ["xiaowennuan-wechat|real@chatroom"]
    assert (wechat["group_id"], wechat["bot_id"]) == ("real@chatroom", "real_bot")
    assert not (tmp_path / "upstream").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("authentication_fails", [True, False])
async def test_wechat_authenticates_and_checks_allowlist_before_bridge(tmp_path, monkeypatch, authentication_fails):
    args = Namespace(config=tmp_path / "config/bridge.local.toml", wechat_config=None,
                     ui=False, mode="live", observe_only=False)
    monkeypatch.setattr(launch, "read_settings", lambda _: (tmp_path, {"allowed_groups": []}, tmp_path / "var/groups"))
    from myatry import bridge, server, wechat_client
    events = []
    class Client:
        group_id = "real@chatroom"
        def __init__(self, settings):
            pass
        async def bind(self):
            events.append("authenticate")
            if authentication_fails:
                raise RuntimeError("authentication failed")
        async def close(self):
            events.append("close")
    monkeypatch.setattr(wechat_client.Settings, "load", lambda _: object())
    monkeypatch.setattr(wechat_client, "NativeClient", Client)
    monkeypatch.setattr(bridge, "Bridge", lambda *a, **kw: pytest.fail("Bridge constructed before authentication"))
    monkeypatch.setattr(server, "create_app", lambda *a: pytest.fail("Bridge background tasks started"))
    with pytest.raises((RuntimeError, ValueError)):
        await launch.wechat(args)
    assert events == ["authenticate", "close"]
