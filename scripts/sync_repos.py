"""Sync portable code from this primary repository into the sibling repos.

myatry is the primary development repository. This script propagates:

- core (src/myatri/**, byte-identical)          -> ../MYATRI/src/myatri/**
- WeChat contract files (byte-identical)        -> ../MYATRI-AstrBot/astrbot_plugin_myatri/**
- app layer (src/myatry/**, package rename)     -> ../MYATRI-AstrBot/src/myatri_bridge/**

Safety rules (see docs/HANDOFF.md):

- Dry-run by default; ``--apply`` writes. Targets are normalized to LF.
- A pair whose target drifted locally (adapted on the target side, e.g. files
  with cleaned AstrBot wording) is reported NEEDS REVIEW and never written.
- The first run on unmanifested pairs only records a baseline: it writes files
  that are missing or already equal, and marks differing ones NEEDS REVIEW.
- Core (src/myatri) changes do NOT reach MYATRI-AstrBot automatically: that
  repo consumes the frozen 0.2.8 wheel; a wheel rebuild is an explicit release
  step and this script only prints a reminder.

Exit code 0 when nothing needs review, 1 otherwise (usable as a CI check).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/sync_manifest.json"

IDENTICAL_PAIRS = [
    ("wechat:versions-module", "src/myatry/wechat_versions.py",
     "astrbot_plugin_myatri/wechat_versions.py"),
    ("wechat:versions-table", "src/myatry/wechat-versions.json",
     "astrbot_plugin_myatri/wechat-versions.json"),
    ("wechat:upgrade-guide", "docs/微信版本升级指南.md", "docs/微信版本升级指南.md"),
    ("wechat:client", "src/myatry/wechat_client.py",
     "astrbot_plugin_myatri/wechatagent_client.py"),
]


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path):
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def rename_imports(text, package):
    text = re.sub(r"\bfrom myatry\.", f"from {package}.", text)
    text = re.sub(r"\bfrom myatry import", f"from {package} import", text)
    text = re.sub(r"\bimport myatry\b", f"import {package}", text)
    # Dynamic module paths inside import_module("...") strings must follow.
    text = text.replace('import_module("myatry.', f'import_module("{package}.')
    return text


def collect(root, base):
    skip = {"__pycache__", "upstream"}
    return sorted(str(p.relative_to(base)) for p in base.rglob("*")
                  if p.is_file() and not skip & set(p.parts) and "__pycache__" not in p.parts)


def build_pairs(kernel, astrbot):
    pairs = []
    for key, src, dst in IDENTICAL_PAIRS:
        pairs.append({"key": key, "source": ROOT / src, "target": astrbot / dst,
                      "transform": None, "layer": "wechat"})
    for rel in collect(None, ROOT / "src/myatri"):
        target = kernel / "src/myatri" / rel
        if target.is_file():
            pairs.append({"key": f"core:{rel}", "source": ROOT / "src/myatri" / rel,
                          "target": target, "transform": None, "layer": "core"})
    for rel in collect(None, ROOT / "src/myatry"):
        target = astrbot / "src/myatri_bridge" / rel
        if target.is_file():
            pairs.append({"key": f"bridge:{rel}", "source": ROOT / "src/myatry" / rel,
                          "target": target, "transform": "myatri_bridge", "layer": "app"})
    return pairs


def kernel_companion_drift(kernel):
    """Report-only: modules that exist both here and in kernel companion/."""
    rows = []
    companion = kernel / "src/myatri/companion"
    if not companion.is_dir():
        return rows
    for rel in collect(None, ROOT / "src/myatry"):
        target = companion / rel
        if target.is_file():
            here, there = read(ROOT / "src/myatry" / rel), read(target)
            if rename_imports(here, "myatri.companion") != there:
                rows.append(rel)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kernel-dir", type=Path, default=ROOT.parent / "MYATRI")
    parser.add_argument("--astrbot-dir", type=Path, default=ROOT.parent / "MYATRI-AstrBot")
    parser.add_argument("--apply", action="store_true", help="写入变更（默认仅预演）")
    args = parser.parse_args()
    kernel, astrbot = args.kernel_dir, args.astrbot_dir
    for name, path in (("MYATRI", kernel), ("MYATRI-AstrBot", astrbot)):
        if not path.is_dir():
            raise SystemExit(f"未找到兄弟仓库 {name}：{path}（可用 --kernel-dir/--astrbot-dir 指定）")

    pairs = build_pairs(kernel, astrbot)
    state = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}
    result, applied, review = {}, [], []

    for pair in pairs:
        source_text = read(pair["source"])
        wanted = rename_imports(source_text, pair["transform"]) if pair["transform"] else source_text
        source_sha, wanted_sha = sha256(source_text), sha256(wanted)
        record = state.get(pair["key"])
        target_text = read(pair["target"]) if pair["target"].is_file() else None
        target_sha = sha256(target_text) if target_text is not None else None
        status = "in_sync" if target_sha == wanted_sha else "pending"

        if target_sha == wanted_sha:
            pass  # nothing to do
        elif record is None:
            # Baseline run: write only missing targets; flag differing ones.
            if target_text is None:
                status = "applied_new"
            else:
                status, target_sha = "needs_review", target_sha
        elif record.get("source_sha") == source_sha:
            status = "needs_review" if target_sha != record.get("target_sha") else "in_sync"
        elif record.get("target_sha") == target_sha:
            status = "applied_update"
            if record.get("status") == "needs_review":
                # The target drift was never resolved; writing would clobber
                # target-side adaptations. Manual resolution required.
                status = "needs_review"
        else:
            status = "needs_review"

        if status in {"applied_new", "applied_update"} and args.apply:
            pair["target"].parent.mkdir(parents=True, exist_ok=True)
            pair["target"].write_text(wanted, encoding="utf-8", newline="\n")
            applied.append(f"{pair['key']} -> {pair['target']}")
        elif status == "applied_new" or (status == "applied_update" and not args.apply):
            status = "pending"
        result[pair["key"]] = {"source_sha": source_sha, "target_sha": target_sha,
                               "status": status, "layer": pair["layer"]}
        if status == "needs_review":
            review.append(pair["key"])

    drift = kernel_companion_drift(kernel)
    core_changed = any(row["status"] in {"applied_new", "applied_update"} and row["layer"] == "core"
                       for row in result.values())

    print(f"配对总数 {len(result)}；本轮{'写入' if args.apply else '预演'} {len(applied)} 项；需人工核对 {len(review)} 项")
    for line in applied:
        print(f"  已写入 {line}")
    for key in review:
        print(f"  NEEDS REVIEW {key}（目标侧有本地改动，见 docs/sync_manifest.json）")
    if drift:
        print(f"内核 companion/ 报告级漂移 {len(drift)} 个模块（不自动写入）：")
        for rel in drift[:10]:
            print(f"  drift {rel}")
    if core_changed:
        print("提醒：核心 src/myatri 有更新；MYATRI-AstrBot 使用冻结 0.2.8 wheel，"
              "需要显式重建 wheel 才能获得这些改动。")

    if args.apply:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8", newline="\n")
        print(f"清单已更新：{MANIFEST.relative_to(ROOT)}")
    else:
        print("（dry-run：加 --apply 写入并更新清单）")
    return 1 if review else 0


if __name__ == "__main__":
    sys.exit(main())
