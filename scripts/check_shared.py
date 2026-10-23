"""Cross-repo consistency checks after auto-sync removal (2026-10-24).

All three repositories stay active and evolve independently. The only invariant that
must still hold between the two WeChat projects is byte-identity of the
WeChat version compatibility table (and its upgrade guide), because both
consume the same external WeChatAgent runtime.

Read-only; exit 0 when consistent, 1 otherwise. Sibling locations can be
overridden with --astrbot-dir.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

SHARED_FILES = [
    ("src/myatry/wechat-versions.json", "astrbot_plugin_myatri/wechat-versions.json"),
    ("docs/微信版本升级指南.md", "docs/微信版本升级指南.md"),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--astrbot-dir", type=Path, default=ROOT.parent / "MYATRI-AstrBot")
    args = parser.parse_args()
    astrbot = args.astrbot_dir
    if not astrbot.is_dir():
        print(f"未找到 MYATRI-AstrBot 仓库：{astrbot}")
        return 1
    failures = 0
    for here, there in SHARED_FILES:
        source, target = ROOT / here, astrbot / there
        if not source.is_file() or not target.is_file():
            print(f"缺少文件：{here} 或 {there}")
            failures += 1
            continue
        if digest(source) != digest(target):
            print(f"不一致：{here} ≠ {there}（微信兼容表/升级指南需两仓手工保持一致）")
            failures += 1
        else:
            print(f"一致：{here}")
    if failures:
        print(f"{failures} 处不一致；见 docs/微信版本升级指南.md 的维护约定。")
        return 1
    print("共享文件全部一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
