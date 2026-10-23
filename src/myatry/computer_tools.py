"""Bounded file and Windows UIA tools. No shell, arbitrary process, external upload or delete."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .dsh_client import INHERITED_ENV, CREATION_FLAGS


def schema(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
    }


STRING = {"type": "string", "maxLength": 500}
TOOLS = [
    schema(
        "files",
        "读取或创建授权根目录下的文件；仅相对路径，不覆盖、不删除，UTF-8文本。",
        {
            "action": {"enum": ["list", "read", "write", "copy", "mkdir"]},
            "root": STRING,
            "path": STRING,
            "destination": STRING,
            "text": {"type": "string", "maxLength": 16000},
        },
        ["action", "root", "path"],
    ),
    schema(
        "desktop",
        "Windows界面元素操作。先windows，再inspect；invoke/set_value使用最新element票据。",
        {
            "action": {"enum": ["windows", "launch", "inspect", "invoke", "set_value"]},
            "application": {"enum": ["notepad", "calculator"]},
            "window": STRING,
            "element": STRING,
            "text": {"type": "string", "maxLength": 2000},
        },
        ["action"],
    ),
    schema(
        "finish_task",
        "记录完成或阻碍；完成必须有独立宿主检查，不能靠文字自证。",
        {
            "status": {"enum": ["completed", "blocked"]},
            "summary": {"type": "string", "maxLength": 1200},
            "checks": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "enum": [
                                "file_exists",
                                "file_contains",
                                "window_exists",
                                "window_contains",
                            ]
                        },
                        "root": STRING,
                        "path": STRING,
                        "text": {"type": "string", "maxLength": 1000},
                        "window": STRING,
                    },
                    "required": ["kind"],
                },
            },
        },
        ["status", "summary", "checks"],
    ),
]


class ComputerTools:
    def __init__(self, roots, *, desktop_enabled=True):
        self.roots = {name: Path(path).absolute() for name, path in roots.items()}
        self.desktop_enabled = desktop_enabled
        self.observed = {}
        self.deadline = None

    def path(self, root, path, *, exists=False):
        if root not in self.roots or not isinstance(path, str) or "\x00" in path:
            raise ValueError("invalid_root_or_path")
        base = self.roots[root]
        relative = Path(path)
        if (
            relative.is_absolute()
            or relative.drive
            or ".." in relative.parts
            or ":" in path
        ):
            raise ValueError("relative_path_required")
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if any(
            part.rstrip(" .") != part or part.split(".")[0].upper() in reserved
            for part in relative.parts
        ):
            raise ValueError("reserved_path")
        target = base / relative
        for parent in (target, *target.parents):
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("linked_path_rejected")
        if not target.resolve().is_relative_to(base.resolve()):
            raise ValueError("outside_task_root")
        if target.is_file() and (
            target.stat().st_nlink > 1 or target.stat().st_size > 2_000_000
        ):
            raise ValueError("file_unavailable")
        if exists and not target.exists():
            raise ValueError("missing_file")
        return target

    def files(self, action, root, path, destination=None, text=None):
        p = self.path(root, path)
        if action == "list":
            if not p.is_dir():
                raise ValueError("not_directory")
            rows = []
            for item in sorted(p.iterdir())[:101]:
                if item.is_symlink() or item.is_junction():
                    continue
                rows.append(
                    {
                        "name": item.name,
                        "directory": item.is_dir(),
                        "bytes": item.stat().st_size if item.is_file() else None,
                    }
                )
            return {"entries": rows[:100], "truncated": len(rows) > 100}
        if action == "mkdir":
            p.mkdir(parents=False, exist_ok=False)
            return {"created": path}
        if action == "read":
            data = self.path(root, path, exists=True).read_bytes()
            value = data.decode("utf-8-sig")
            return {
                "text": value[:16000],
                "truncated": len(value) > 16000,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        if action == "write":
            if not isinstance(text, str) or len(text) > 16000:
                raise ValueError("invalid_text")
            data = text.encode("utf-8")
        elif action == "copy":
            data = self.path(root, path, exists=True).read_bytes()
            p = self.path(root, destination)
        else:
            raise ValueError("unsupported_file_action")
        with p.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        check = p.read_bytes()
        return {
            "written": p.name,
            "bytes": len(check),
            "sha256": hashlib.sha256(check).hexdigest(),
        }

    def _uia(self, request):
        remaining = (
            15 if self.deadline is None else min(15, self.deadline - time.monotonic())
        )
        if remaining <= 0:
            raise ValueError("verification_deadline")
        script = Path(__file__).parent / "harness/windows-uia.ps1"
        env = {
            key: os.environ[key]
            for key in INHERITED_ENV
            if key in os.environ and key != "DSH_HOME"
        }
        power = (
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        result = subprocess.run(
            [str(power), "-NoProfile", "-NonInteractive", "-File", str(script)],
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=remaining,
            shell=False,
            env=env,
            creationflags=CREATION_FLAGS,
        )
        if result.returncode or len(result.stdout) > 100000:
            raise RuntimeError("desktop_outcome_unknown")
        return json.loads(result.stdout.decode("utf-8-sig"))

    def desktop(self, action, application=None, window=None, element=None, text=None):
        if not self.desktop_enabled or os.name != "nt":
            raise ValueError("desktop_unavailable")
        if action == "launch":
            if application not in {"notepad", "calculator"}:
                raise ValueError("application_not_allowed")
            return self._uia({"action": "launch", "application": application})
        if action == "windows":
            return self._uia({"action": "windows"})
        if action == "inspect":
            self.observed.clear()
            result = self._uia({"action": "inspect", "window": window})
            for row in result.get("elements", []):
                ticket = uuid.uuid4().hex
                self.observed[ticket] = {
                    "window": window,
                    "runtime": row.pop("runtime"),
                    "process_start": result["process_start"],
                    "expires": time.monotonic() + 20,
                }
                row["element"] = ticket
            return result
        if action not in {"invoke", "set_value"} or element not in self.observed:
            raise ValueError("inspect_again_required")
        bound = self.observed.pop(element)
        if time.monotonic() > bound.pop("expires"):
            self.observed.clear()
            raise ValueError("inspect_again_required")
        if window != bound["window"]:
            raise ValueError("window_mismatch")
        self.observed.clear()
        return self._uia({**bound, "action": action, "text": text or ""})

    def verify(self, check):
        kind = check["kind"]
        if kind == "file_exists":
            path = self.path(check["root"], check["path"], exists=True)
            return {"kind": kind, "path": check["path"], "passed": path.is_file()}
        if kind == "file_contains":
            text = check.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("nonempty_expected_text_required")
            path = self.path(check["root"], check["path"], exists=True)
            return {
                "kind": kind,
                "path": check["path"],
                "passed": text in path.read_text(encoding="utf-8-sig"),
            }
        if kind == "window_exists":
            windows = self.desktop("windows")["windows"]
            return {
                "kind": kind,
                "window": check["window"],
                "passed": any(w["window"] == check["window"] for w in windows),
            }
        if kind == "window_contains":
            expected = check.get("text")
            if not isinstance(expected, str) or not expected:
                raise ValueError("nonempty_expected_text_required")
            inspected = self.desktop("inspect", window=check["window"])
            texts = [
                row.get("name", "") + "\n" + row.get("value", "")
                for row in inspected.get("elements", [])
            ]
            return {
                "kind": kind,
                "window": check["window"],
                "passed": any(expected in t for t in texts),
            }
        raise ValueError("unsupported_check")
