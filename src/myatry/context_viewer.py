"""Owner-requested local inspection of captured inputs, separate from metrics."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hmac
import http.client
import json
from pathlib import Path
import re
import secrets
import sqlite3
import webbrowser

import httpx
from aiohttp import web

from .monitor import _connect, _read_config, _usage_view


_KEY = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _redact(value):
    if isinstance(value, str):
        return _KEY.sub("[已隐藏密钥]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()
                if key not in {"reasoning_content", "provider_fields", "api_key", "authorization", "headers"}}
    return value


def libraries(project):
    project = Path(project).resolve()
    settings = _read_config(project / "config/bridge.local.toml").get("bridge", {})
    state = (project / settings.get("state_root", "var/groups")).resolve()
    if not state.is_relative_to(project):
        raise ValueError("invalid group root")
    found = {}
    if state.is_dir():
        for path in sorted(state.iterdir()):
            if (path.is_dir() and path.resolve().is_relative_to(state)
                    and (path / "runtime.sqlite3").is_file() and (path / "domains").is_dir()):
                index = len(found) + 1
                found[f"group-{index}"] = (path, f"微信群聊 {index}")
    return found


def capture_files(path):
    directory = path / "context-debug"
    if not directory.is_dir() or not directory.resolve().is_relative_to(path.resolve()):
        return []
    files = [p for p in directory.glob("*.json") if re.fullmatch(r"[a-f0-9]{64}\.json", p.name)
             and p.is_file() and not p.is_symlink()]
    return sorted(files, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:5]


def _captures(path):
    result = []
    for file in capture_files(path):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            result.append((file.stem, data))
    return result


def _checkpoint(path):
    runtime = path / "runtime.sqlite3"
    if not runtime.is_file():
        return None, None
    latest = None
    try:
        with closing(_connect(runtime)) as db:
            for (body,) in db.execute("SELECT payload FROM protocols ORDER BY rowid DESC LIMIT 1000"):
                try:
                    row = json.loads(body)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                if latest is None:
                    latest = {key: row.get(key) for key in ("status", "sent_at", "replied_at")}
                if isinstance(row.get("messages"), list):
                    return row, latest
    except (sqlite3.Error, OSError):
        pass
    return None, latest


def _category(message):
    content = message.get("content", "") or ""
    if message.get("role") == "tool":
        return "tool", "工具返回"
    for prefix, category, title in (
        ("本轮接话重点", "focus", "本轮接话重点：背景不等于待回复任务"),
        ("群聊渠道规则", "group_policy", "群规则、成员资料与短期摘要"),
        ("回忆材料", "recall", "检索召回材料"),
        ("会话语义摘要", "summary", "会话摘要"),
        ("会话来源摘录", "summary", "会话来源摘录"),
        ("用户授权迁入", "imported_history", "授权继承的私聊历史"),
        ("继承历史档案", "imported_history", "继承历史"),
        ("当前暂定状态", "state", "暂定状态与心情"),
        ("[近期群聊背景", "timeline", "当前消息与旁听时间线"),
        ("本轮当前身份", "persona", "当前身份与表达规则"),
    ):
        if content.startswith(prefix):
            return category, title
    if message.get("role") in {"user", "assistant"}:
        return "dialogue", "已有对话" if message.get("role") == "user" else "助手历史回复"
    return "persona" if "当前人格（" in content else "rules", "角色与规则"


def _parsed(content):
    candidates = [content]
    if content.startswith("[近期群聊背景") and "\n" in content:
        candidates.insert(0, content.split("\n", 1)[1])
    if "<group_channel_data>\n" in content:
        candidates.insert(0, content.split("<group_channel_data>\n", 1)[1].split("\n</group_channel_data>", 1)[0])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, (dict, list)):
            return value
    return None


def _messages(messages):
    result = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        category, title = _category({**message, "content": content})
        row = {"index": index, "role": message.get("role", "unknown"), "category": category,
               "title": title, "content": content}
        for key in ("tool_calls", "tool_call_id", "name"):
            if key in message:
                row[key] = message[key]
        parsed = _parsed(content)
        if parsed is not None:
            row["parsed"] = parsed
        result.append(_redact(row))
    return result


def _receipt(path, trace_id):
    if not trace_id or not (path / "runtime.sqlite3").is_file():
        return None
    try:
        with closing(_connect(path / "runtime.sqlite3")) as db:
            for (body,) in db.execute("SELECT payload FROM usage ORDER BY id DESC LIMIT 300"):
                try:
                    row = json.loads(body)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict) and row.get("trace_id") == trace_id:
                    return _usage_view(row)
    except (sqlite3.Error, OSError):
        pass
    return None


def library_list(project):
    result = []
    for key, (path, label) in libraries(project).items():
        captures = _captures(path)
        checkpoint, _ = _checkpoint(path)
        result.append({"key": key, "label": label, "capture_available": bool(captures),
            "checkpoint_available": checkpoint is not None,
            "runtime_controls": key.startswith("group-") and bool(re.fullmatch(r"[a-f0-9]{32}", path.name)),
            "latest_capture_at": captures[0][1].get("captured_at") if captures else None,
            "latest_checkpoint_at": (checkpoint.get("sent_at") if checkpoint else None)})
    return {"libraries": result}


async def proxy_control(project, library, action):
    """Use the authenticated running bridge; never edit its databases here."""
    project = Path(project).resolve()
    found = libraries(project)
    if library not in found:
        return {"error": "未找到选中的会话库。"}, 404
    path, _ = found[library]
    if not library.startswith("group-") or not re.fullmatch(r"[a-f0-9]{32}", path.name):
        return {"error": "当前操作支持运行中的微信群会话；私聊请重新启动对应窗口。"}, 400
    settings = _read_config(project / "config/bridge.local.toml").get("bridge", {})
    host, port = settings.get("host", "127.0.0.1"), settings.get("port", 8766)
    token_file = settings.get("token_file", "var/bridge-token.txt")
    if host not in {"127.0.0.1", "localhost"} or type(port) is not int or not 1 <= port <= 65535:
        return {"error": "机器人服务地址配置无效，只能连接本机。"}, 503
    if not isinstance(token_file, str):
        return {"error": "本机桥接认证配置不可用，请从原入口启动机器人。"}, 503
    token_path = (project / token_file).resolve()
    if not token_path.is_relative_to(project):
        return {"error": "本机桥接认证配置不可用，请从原入口启动机器人。"}, 503
    try:
        token = token_path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return {"error": "本机桥接认证配置不可用，请从原入口启动机器人。"}, 503
    if len(token) < 32:
        return {"error": "本机桥接认证配置不可用，请从原入口启动机器人。"}, 503
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = await client.post(f"http://{host}:{port}/v1/admin/{action}",
                headers={"Authorization": "Bearer " + token}, json={"scope": path.name})
    except httpx.TimeoutException:
        return {"error": "操作响应超时，结果尚未确认。请稍后检查，勿连续点击。"}, 504
    except httpx.HTTPError:
        return {"error": "无法连接机器人服务。请从原入口启动或重启机器人后再试。"}, 503
    if response.status_code == 409:
        return {"error": "当前有回复正在生成或发送，请结束后再试。"}, 409
    if response.status_code in {401, 403}:
        return {"error": "机器人服务认证失败，请重新启动机器人和查看器。"}, 503
    if response.status_code == 404:
        return {"error": "机器人尚未载入该会话，或服务版本较旧；请重启机器人后再试。"}, 404
    if not response.is_success:
        return {"error": "机器人未完成操作，请检查运行窗口中的错误信息。"}, 502
    try:
        data = response.json()
    except ValueError:
        return {"error": "机器人返回了无法确认的操作结果。"}, 502
    if not isinstance(data, dict) or data.get("ok") is not True or data.get("scope") != path.name:
        return {"error": "机器人返回了无法确认的操作结果。"}, 502
    allowed = {key: data[key] for key in ("ok", "action", "scope", "persona_ref", "persona_changed",
               "prompts_reloaded", "context_reset") if key in data}
    return _redact(allowed), 200


def context_data(project, library, source="latest", capture=""):
    found = libraries(project)
    if library not in found:
        raise KeyError(library)
    path, label = found[library]
    captures = _captures(path)
    available = [{"key": key, **{field: data.get(field) for field in ("trace_id", "purpose", "captured_at")}}
                 for key, data in captures]
    metadata = {"available_captures": available, "label": label}
    limitations = ["只展示请求材料，不展示模型内部想法或私密推理；明显密钥及非请求扩展字段已隐藏。"]
    if source == "latest":
        match = next((data for key, data in captures if not capture or key == capture), None)
        if match is None:
            return {"status": "not_captured", "source": source, "metadata": metadata, "messages": [], "tools": [],
                "limitations": [*limitations, "尚无可用的请求捕获。请重启机器人后正常聊天一轮；旧完整记录可切换到历史快照。"]}
        messages = match["messages"]
        receipt = _receipt(path, match.get("trace_id"))
        metadata.update(receipt=receipt, limits=match.get("limits"), redaction=match.get("redaction"))
        local_metadata = match.get("metadata") if isinstance(match.get("metadata"), dict) else {}
        metadata["selection"] = local_metadata.get("history_selection", [])
        metadata["context_stats"] = local_metadata.get("context_stats")
        metadata["selection_note"] = "这是本地选材记录，不是模型内部思考；当前话题提示、同一成员与字面重合均为启发式线索，不保证语义相关。"
        limitations.append("这是交给模型供应商前的请求副本；是否收到响应以关联回执为准。后续审查请求会单独保留。")
        result = {key: match.get(key) for key in ("captured_at", "trace_id", "purpose", "attempt", "model")}
        tools = match.get("tools", [])
    elif source == "checkpoint":
        checkpoint, latest = _checkpoint(path)
        metadata["latest_protocol"] = latest
        if checkpoint is None:
            return {"status": "not_captured", "source": source, "messages": [], "tools": [], "metadata": metadata,
                    "limitations": [*limitations, "没有保存过完整历史协议，无法反推旧请求。重启后捕获新的实际请求即可查看。"]}
        messages = list(checkpoint["messages"])
        if (checkpoint.get("status") == "completed" and messages and messages[-1].get("role") == "assistant"
                and not messages[-1].get("tool_calls")):
            messages.pop()  # This generated answer was not part of its own input.
        result = {"captured_at": checkpoint.get("sent_at"), "trace_id": None,
                  "purpose": "historical_conversation_checkpoint", "attempt": None}
        tools = []
        limitations.extend(["历史快照：由当时保存的完整协议还原最后一次主对话输入，不等于当前最新上下文。",
            "旧协议未保存当时工具定义或额外回复审查请求；这些部分不能可靠恢复。"])
    else:
        raise ValueError("invalid source")
    projected = _messages(messages)
    selected = {row.get("message_id"): row for row in metadata.get("selection", [])
                if isinstance(row, dict) and row.get("message_id")}
    for row in projected:
        parsed = row.get("parsed")
        if isinstance(parsed, dict) and parsed.get("message_id") in selected:
            row["selection"] = {**selected[parsed["message_id"]],
                "note": "本地选材依据，不是模型内部想法；同一成员、近期或字面重合不等于语义相关。"}
        elif row["category"] == "timeline":
            row["selection"] = {"reasons": ["current_message", "recent_group_window"],
                "note": "当前消息必带；旁听按近期窗口进入，并未逐条做模型语义判别，可能包含并行话题。"}
    return _redact({**result, "status": "ok", "source": source, "metadata": metadata,
        "messages": projected, "tools": tools, "message_count": len(projected),
        "total_chars": sum(len(m["content"]) for m in projected), "limitations": limitations})


def create_app(project):
    session = secrets.token_urlsafe(32)

    @web.middleware
    async def local_only(request, handler):
        host = request.host.rsplit(":", 1)[0]
        if host not in {"127.0.0.1", "localhost"}:
            raise web.HTTPForbidden()
        if request.headers.get("Origin") not in {None, f"http://{request.host}"}:
            raise web.HTTPForbidden()
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden()
        control = request.path in {"/api/admin/reload", "/api/admin/clear-context"}
        if request.method != "GET" and not (request.method == "POST" and control):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"] if control else ["GET"])
        if request.path.startswith("/api/") and not hmac.compare_digest(request.cookies.get("context_view_session", ""), session):
            raise web.HTTPForbidden()
        if request.method == "POST" and (request.headers.get("Origin") != f"http://{request.host}"
                or request.headers.get("X-Context-Control") != "1"):
            raise web.HTTPForbidden()
        response = await handler(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-MYATRI-Context-Viewer": "1",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "no-referrer"})
        return response

    async def index(request):
        response = web.Response(text=Path(__file__).with_name("context_viewer.html").read_text(encoding="utf-8"), content_type="text/html")
        response.set_cookie("context_view_session", session, httponly=True, samesite="Strict")
        return response

    async def list_libraries(request):
        return web.json_response(await asyncio.to_thread(library_list, project))

    async def show_context(request):
        try:
            result = await asyncio.to_thread(context_data, project, request.query.get("library", ""),
                request.query.get("source", "latest"), request.query.get("capture", ""))
        except KeyError:
            raise web.HTTPNotFound() from None
        except ValueError:
            raise web.HTTPBadRequest() from None
        return web.json_response(result)

    async def control(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(body, dict) or set(body) != {"library"} or not isinstance(body["library"], str):
            raise web.HTTPBadRequest()
        action = request.path.rsplit("/", 1)[1]
        data, status = await proxy_control(project, body["library"], action)
        return web.json_response(data, status=status)

    app = web.Application(middlewares=[local_only])
    app.router.add_get("/", index)
    app.router.add_get("/api/libraries", list_libraries)
    app.router.add_get("/api/context", show_context)
    app.router.add_post("/api/admin/reload", control)
    app.router.add_post("/api/admin/clear-context", control)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if args.open_browser:
        # Reuse this viewer when the user double-clicks its launcher again.
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=.5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            if response.getheader("X-MYATRI-Context-Viewer") == "1":
                webbrowser.open(f"http://127.0.0.1:{args.port}/")
                return
        except OSError:
            pass
        finally:
            connection.close()
    app = create_app(args.project)
    if args.open_browser:
        async def open_browser(app):
            asyncio.get_running_loop().call_later(.5, webbrowser.open, f"http://127.0.0.1:{args.port}/")
        app.on_startup.append(open_browser)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)
