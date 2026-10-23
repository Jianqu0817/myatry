"""Use the upgraded project-local Harness with its own home and workspace."""
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / 'config/bridge.local.toml'
    config = tomllib.loads(path.read_text(encoding="utf-8-sig"))['bridge']["computer"]
    runtime = Path(config["runtime_root"])
    cli = runtime / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    workspace = ROOT / "var/harness/workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DSH_HOME=str(ROOT / "var/harness/home"))
    args = sys.argv[1:] or ["--version"]
    raise SystemExit(subprocess.run([config["node_path"], str(cli), *args], cwd=workspace, env=env).returncode)


if __name__ == "__main__":
    main()
