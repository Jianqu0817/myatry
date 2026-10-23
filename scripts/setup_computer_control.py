"""Install the pinned local Harness and bind computer actions to the existing owner.

No credentials, model calls, service restart or messaging. Run with bridge Python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def replace_table(text, name, values):
    table = "[" + name + "]\n" + "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()) + "\n\n"
    pattern = r"(?m)^\[" + re.escape(name) + r"\][^\n]*\n[\s\S]*?(?=^\[|\Z)"
    return re.sub(pattern, lambda _: table, text, count=1) if re.search(pattern, text) else text.rstrip() + "\n\n" + table


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--pnpm-js", required=True)
    parser.add_argument('--full',action='store_true',help='Enable the full native Harness execution agent')
    args = parser.parse_args()
    table = 'bridge'
    local = ROOT / 'config/bridge.local.toml'
    text = local.read_text(encoding="utf-8-sig")
    original = tomllib.loads(text)
    bridge = original[table]
    if not bridge.get("owner_id") or bridge.get("owner_name") != "菅蘧":
        raise ValueError("A manually bound owner identity for 菅蘧 is required")
    current = bridge.get("computer", {})
    if current.get("owner_id") and current["owner_id"] != bridge["owner_id"]:
        raise ValueError("Existing computer owner binding differs; inspect before changing identity")
    runtime = ROOT / "var/harness/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    node = ROOT / "var/harness/node/node.exe"
    node.parent.mkdir(parents=True, exist_ok=True)
    source_node = Path(args.node).resolve()
    if source_node != node.resolve():
        shutil.copyfile(source_node, node)
    for name in ("package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml"):
        source = ROOT / "config/harness" / name
        if not source.exists():
            raise FileNotFoundError(f"Missing pinned Harness manifest: {name}")
        shutil.copyfile(source, runtime / name)
    subprocess.run([str(node), str(Path(args.pnpm_js).resolve()), "--dir", str(runtime),
                    "install", "--frozen-lockfile"], check=True, timeout=300)
    cli = runtime / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    version = subprocess.check_output([str(node), str(cli), "--version"], text=True, timeout=20).strip()
    if version != "0.2.1-rc.2":
        raise RuntimeError("Harness version mismatch")
    from myatry.computer_control import HarnessExecutor
    executor = HarnessExecutor(node, runtime, ROOT / "var/computer-control/notes")
    probe = executor.run("0" * 64, {"action": "probe", "text": ""})
    if probe.get("status") != "ready":
        raise RuntimeError("Harness execution composition did not pass its offline probe")
    settings = {**current,"enabled": True, "owner_id": bridge["owner_id"], "node_path": str(node), "runtime_root": str(runtime)}
    if args.full:
        settings.update(agent_enabled=True,permission_profile='full',native_config='config/harness/executor.local.json')
    updated = replace_table(text, table+".computer", settings)
    expected = json.loads(json.dumps(original))
    expected[table]["computer"] = settings
    if tomllib.loads(updated) != expected:
        raise RuntimeError("Unexpected changes outside computer configuration")
    # Do not overwrite a concurrent configuration edit after a long installation.
    if local.read_text(encoding="utf-8-sig") != text:
        raise RuntimeError("Local configuration changed during installation; retry after inspection")
    temporary = local.with_suffix(".toml.computer.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(local)
    installed = {"harness_version": version, "owner_name": "菅蘧",
        "owner_id_sha256": hashlib.sha256(bridge["owner_id"].encode()).hexdigest(),
        "node_version": subprocess.check_output([str(node), "--version"], text=True).strip(),
        "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "runtime": str(runtime), "probe": probe, "restarted_services": False,
        "pid": os.getpid()}
    (ROOT / "var/harness/installation.json").write_text(json.dumps(installed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"installed": version, "owner": "菅蘧", "probe": probe, "restart_required": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()

