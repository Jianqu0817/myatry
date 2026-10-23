"""WeChat client version compatibility table shared across MYATRI projects.

The single data source is wechat-versions.json next to this module; keep the
file byte-identical in every consuming repository (checked by sync tooling).
To support a newer WeChat after an upstream WeChatAgent release ships a
matching hook build, add a table entry and follow docs/微信版本升级指南.md —
no code changes are required in the check paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

TABLE_FILE = Path(__file__).with_name("wechat-versions.json")
UPGRADE_GUIDE = "docs/微信版本升级指南.md"


@dataclass(frozen=True)
class WeChatVersionSpec:
    wechat_version: str
    client_dir: str
    wechatagent_runtime: str
    integration: str
    backend: str
    notes: str = ""


def _load():
    rows = json.loads(TABLE_FILE.read_text(encoding="utf-8")).get("versions", [])
    specs = {}
    for row in rows:
        spec = WeChatVersionSpec(
            wechat_version=str(row["wechat_version"]),
            client_dir=str(row["client_dir"]),
            wechatagent_runtime=str(row["wechatagent_runtime"]),
            integration=str(row["integration"]),
            backend=str(row["backend"]),
            notes=str(row.get("notes", "")),
        )
        specs[spec.wechat_version] = spec
    if not specs:
        raise RuntimeError("wechat-versions.json 未包含任何版本条目")
    return specs


SUPPORTED = _load()


def supported_versions():
    return list(SUPPORTED)


def spec_for(version):
    return SUPPORTED.get(version) if isinstance(version, str) else None
