"""Loopback-only control proxy. The browser never receives the bridge token."""
from pathlib import Path
import tomllib

import httpx


async def runtime_settings_proxy(project, payload=None, *, task_query=None, computer=False):
    project = Path(project).resolve()
    try:
        settings = tomllib.loads((project / "config/bridge.local.toml").read_text(encoding="utf-8-sig"))["bridge"]
        port = settings.get("port", 8766)
        if settings.get("host", "127.0.0.1") != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid loopback destination")
        token_path = (project / settings["token_file"]).resolve()
        if not token_path.is_relative_to(project):
            raise ValueError("invalid token location")
        token = token_path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise ValueError("invalid token")
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.request("GET" if payload is None else "POST",
                f"http://127.0.0.1:{port}/v1/admin/"+('computer-tasks' if computer else 'runtime-settings'),
                headers={"Authorization": "Bearer " + token},
                **({'params':{'id':task_query}} if task_query else {}),
                **({"json": payload} if payload is not None else {}))
        if response.status_code == 409:
            return {"error": "设置已被其他窗口修改，请重新读取后保存。"}, 409
        if not response.is_success:
            return {"error": "机器人未提供新版设置接口，请正常重启桥接服务后再试。"}, 503
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("unconfirmed response")
        if computer:
            return data,200
        from .server import RuntimeSettingsUpdate
        view = RuntimeSettingsUpdate.model_validate({k: v for k, v in data.items() if k != "ok"})
        return {"ok": True, **view.model_dump()}, 200
    except httpx.TimeoutException:
        return {"error": "操作超时，保存结果未确认；请重新读取设置，勿连续保存。"}, 504
    except (OSError, ValueError, KeyError, TypeError, AttributeError, httpx.HTTPError):
        return {"error": "无法连接本机新版机器人服务，请检查其运行状态。"}, 503
