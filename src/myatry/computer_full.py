"""Full-account task profile: official Harness tools plus host-backed completion evidence."""

from copy import deepcopy
import json
import re
from pathlib import Path

from .computer_tools import ComputerTools, TOOLS

FULL_FINISH = deepcopy(TOOLS[-1])
check = FULL_FINISH["parameters"]["properties"]["checks"]["items"]["properties"]
check["kind"]["enum"] += ["receipt_success", "receipt_contains"]
check["call_id"] = {"type": "string", "maxLength": 500}
FULL_FINISH["description"] = (
    "结束通用电脑任务；completed的checks不得为空。用真实工具回执或文件检查验证结果。"
    "receipt_success/receipt_contains的call_id必须逐字复制工具结果旁的host_receipt_id，不能自造pwsh-1。"
    "校验失败按返回的available_receipt_ids修正引用，不要重复已成功的操作。"
)


class FullComputerTools(ComputerTools):
    def __init__(self, roots, store, task):
        super().__init__(roots)
        self.store, self.task = store, task

    def path(self, root, path, *, exists=False):
        if not isinstance(path, str) or "\x00" in path:
            raise ValueError("invalid_path")
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.roots[root or "workspace"] / target
        target = target.resolve()
        if exists and not target.exists():
            raise ValueError("missing_file")
        return target

    def verify(self, check):
        if check["kind"] in {"receipt_success", "receipt_contains"}:
            result = self.store.result(self.task, check["call_id"])
            passed = result.get("isError") is False
            # Canonical shell failure may be a successful tool projection with a nonzero process exit.
            value = result.get("value") or {}
            if isinstance(value, dict) and value.get("exitCode") not in (None, 0):
                passed = False
            if isinstance(value, dict):
                if value.get("kind") in {"background", "promoted"}:
                    passed = False
                if value.get("kind") == "foreground" and (
                    value.get("exitCode") != 0
                    or value.get("timedOut")
                    or value.get("aborted")
                ):
                    passed = False
                if (
                    isinstance(value.get("job"), dict)
                    and value["job"].get("status") != "completed"
                ):
                    passed = False
                if isinstance(value.get("job"), dict):
                    exit_code = re.search(
                        r"exit code:\s*(-?\d+)", value["job"].get("detail", "")
                    )
                    if exit_code and int(exit_code[1]) != 0:
                        passed = False
            if check["kind"] == "receipt_contains":
                expected = check.get("text")
                if not isinstance(expected, str) or not expected:
                    raise ValueError("expected_text_required")
                passed = passed and expected in json.dumps(result, ensure_ascii=False)
            return {
                "kind": check["kind"],
                "call_id": check["call_id"],
                "passed": passed,
            }
        return super().verify({"root": "workspace", **check})
