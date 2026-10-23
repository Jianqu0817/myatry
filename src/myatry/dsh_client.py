"""调用 DeepSeek Harness 无头模式的子进程封装。

只做一件事：用 :mod:`subprocess` 启动一次 ``dsh --profile <name> <task>``，
拿回 stdout/stderr 与退出码。超时、无 shell、环境变量清洗、工作目录隔离都在
这里完成；业务结果（搜索结果 JSON）的解析由 ``web_tools`` 负责。

本机已核实的无头入口（仅作文档，不作为默认值，由调用方显式传入）::

    node   ~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe
    bin.js ~/.dsh/profiles/node_modules/@deepseek-ai/dsh/lib/bin.js

``--help`` 这类不发起模型请求的调用可用于启动自检（见 :meth:`DshClient.probe`）。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_TIMEOUT_SECONDS = 25
"""单次调用的默认超时；超时后 kill 子进程并返回 ``search_timeout``。"""

DEFAULT_PROFILE = "headless"
"""无头模式的内置 profile 名（``dsh --profile headless``）。"""

DEFAULT_PERMISSION_MODE = "read-only"
"""子进程强制使用的权限预设；本机默认的 danger-full-access 一律拒绝。"""

ERROR_TIMEOUT = "search_timeout"
ERROR_BACKEND = "search_backend_failed"
"""本模块只产出这两个错误码，其余错误码由调用方补充。"""

INHERITED_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "DSH_HOME",
)
"""允许从父进程透传的环境变量名；只保留进程启动与本地路径必需项。"""

SENSITIVE_ENV = re.compile(r"(_KEY|_TOKEN|_SECRET|_PASSWORD|PASSWORD)$|PROXY", re.I)
"""永远不得从父进程透传的变量名模式：凭据与代理。"""

PROJECT_MARKERS = (".git", "pyproject.toml", "AGENTS.md", ".env", "uv.lock")
"""出现任一标记即视为项目仓库目录，不能作为子进程工作目录。"""

CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
"""Windows 下不弹控制台窗口；桥接进程常以服务方式运行。"""

KILL_GRACE_SECONDS = 5.0
MAX_TASK_CHARS = 4000
MAX_CAPTURE_CHARS = 200_000

PERMITTED_MODES = ("read-only", "workspace-write")
"""允许的权限预设；``danger-full-access`` 是本机默认值，必须被拒绝。"""


@dataclass(frozen=True, slots=True)
class DshResult:
    """一次无头调用的结果。

    ``error`` 为 ``None`` 表示退出码 0；否则是 ``search_timeout`` 或
    ``search_backend_failed``。超时不会抛异常，而是返回 ``timed_out=True``
    的失败结果。
    """

    ok: bool
    error: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    output_truncated: bool = False


def default_workdir(root: str | Path) -> Path:
    """返回默认的隔离工作目录 ``<root>/var/agents/websearch``。

    调用方仍需把它显式传给 :class:`DshClient`，默认值不会自动生效。
    """
    return Path(root) / "var" / "agents" / "websearch"


class DshClient:
    """DeepSeek Harness 无头模式的子进程客户端。

    构造时不写盘；工作目录在第一次 :meth:`run` 时才创建，因此未配置时不会产生
    任何副作用。查询词放在 argv 里（stdlib 会做 Windows 参数转义，``shell=False``
    不会经过任何 shell），单次调用一个子进程、一次任务。
    """

    def __init__(
        self,
        *,
        node_path: str | Path,
        bin_path: str | Path,
        workdir: str | Path,
        profile: str = DEFAULT_PROFILE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        extra_args: tuple[str, ...] = (),
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        if permission_mode not in PERMITTED_MODES:
            raise ValueError("权限预设必须是 read-only 或 workspace-write")
        if type(timeout_seconds) not in (int, float) or not timeout_seconds > 0:
            raise ValueError("timeout_seconds 必须是正数")
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("profile 不能为空")
        self.node_path = Path(node_path)
        self.bin_path = Path(bin_path)
        self.profile = profile
        self.timeout_seconds = float(timeout_seconds)
        self.permission_mode = permission_mode
        self.extra_args = tuple(str(argument) for argument in extra_args)
        self.extra_env = dict(extra_env or {})
        self.workdir = self._resolve_workdir(workdir)

    @staticmethod
    def _resolve_workdir(workdir: str | Path) -> Path:
        """拒绝把项目仓库或当前进程目录当作子进程工作目录。"""
        candidate = Path(workdir).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as error:  # 无法解析的路径不可能安全
            raise ValueError("无法解析工作目录") from error
        if resolved == Path.cwd().resolve():
            raise ValueError("工作目录不能是当前进程目录")
        if resolved.exists():
            if not resolved.is_dir():
                raise ValueError("工作目录必须是目录")
            for marker in PROJECT_MARKERS:
                if (resolved / marker).exists():
                    raise ValueError("工作目录不得是项目仓库目录")
        return resolved

    def describe_command(self) -> list[str]:
        """返回不含任务文本的 argv 模板，供诊断日志使用。"""
        return [
            str(self.node_path),
            str(self.bin_path),
            "--profile",
            self.profile,
            *self.extra_args,
        ]

    def child_environment(self) -> dict[str, str]:
        """构造子进程环境变量：白名单透传 + 显式附加 + 固定权限预设。

        父进程里任何 ``*_KEY``/``*_TOKEN``/``*_SECRET`` 或代理变量都不会出现；
        ``extra_env`` 是调用方从自己的配置里显式传入的后端凭据，不从环境继承。
        """
        environment = {
            name: os.environ[name]
            for name in INHERITED_ENV
            if name in os.environ and not SENSITIVE_ENV.search(name)
        }
        for name, value in self.extra_env.items():
            if not isinstance(name, str) or not name or not isinstance(value, str):
                raise ValueError("extra_env 必须是 str 到 str 的映射")
            environment[name] = value
        # 强制覆盖，绝不沿用父进程里可能存在的 danger-full-access。
        environment["DSH_PERMISSION_MODE"] = self.permission_mode
        return environment

    def run(self, task: str, *, timeout_seconds: float | None = None) -> DshResult:
        """执行一次无头任务。

        ``timeout_seconds`` 覆盖构造时的默认值；超时会 kill 子进程并返回
        ``error == "search_timeout"``，不抛异常。退出码非零返回
        ``error == "search_backend_failed"``。
        """
        if not isinstance(task, str) or not task.strip():
            raise ValueError("任务文本不能为空")
        if len(task) > MAX_TASK_CHARS:
            raise ValueError("任务文本过长")
        if task.lstrip().startswith("-"):
            # 位置参数会被无头模式当成命令行选项，这里直接拒绝而不是拼 `--`。
            raise ValueError("任务文本不能以 - 开头")
        timeout = self._timeout(timeout_seconds)
        return self._execute([task], timeout)

    def probe(self, *, timeout_seconds: float | None = None) -> DshResult:
        """跑一次 ``--profile <name> --help``，用于启动自检。

        该调用不会发起模型请求，只验证入口、Node 路径与 profile 是否可用。
        """
        timeout = self._timeout(timeout_seconds)
        return self._execute(["--help"], timeout)

    def _timeout(self, timeout_seconds: float | None) -> float:
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if not timeout > 0:
            raise ValueError("timeout_seconds 必须是正数")
        return timeout

    def _execute(self, arguments: list[str], timeout: float) -> DshResult:
        """真正启动子进程：固定 argv、``shell=False``、显式超时并 kill。"""
        self.workdir.mkdir(parents=True, exist_ok=True)
        argv = [*self.describe_command(), *arguments]
        started = time.monotonic()
        process = subprocess.Popen(  # noqa: S603 - 固定 argv、shell=False、无字符串拼接
            argv,
            cwd=str(self.workdir),
            env=self.child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=CREATION_FLAGS,
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # 理论上不会发生：已经 kill
                stdout_bytes, stderr_bytes = b"", b""
        duration = time.monotonic() - started
        stdout, stdout_truncated = _decode(stdout_bytes)
        stderr, stderr_truncated = _decode(stderr_bytes)
        exit_code = process.returncode
        if timed_out:
            error = ERROR_TIMEOUT
        elif exit_code != 0:
            error = ERROR_BACKEND
        else:
            error = None
        return DshResult(
            ok=error is None,
            error=error,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_seconds=duration,
            output_truncated=stdout_truncated or stderr_truncated,
        )


def _decode(data: bytes | None) -> tuple[str, bool]:
    """按 UTF-8 解码子进程输出并限制内存占用，返回 (文本, 是否被截断)。"""
    if not data:
        return "", False
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_CAPTURE_CHARS:
        return text[:MAX_CAPTURE_CHARS], True
    return text, False
