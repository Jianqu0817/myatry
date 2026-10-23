"""Run only the configured external reader in the foreground."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib


def reader_command(config):
    file = Path(config).resolve()
    data = tomllib.loads(file.read_text(encoding="utf-8-sig"))["wechatagent"]
    if not data.get("project_dir"):
        raise ValueError("请先配置wechatagent.project_dir。")
    workspace = Path(data["project_dir"])
    if not workspace.is_absolute():
        workspace = file.parent.parent / workspace
    workspace = workspace.resolve()
    distribution = json.loads((workspace / "config/distribution.json").read_text(encoding="utf-8-sig"))
    python = Path(distribution["python"])
    if not python.is_absolute():
        python = workspace / python
    return [str(python), "-B", str(workspace / "scripts/reader-api.py")], workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/wechatagent.local.toml"))
    args = parser.parse_args(argv)
    command, workspace = reader_command(args.config)
    process = subprocess.Popen(command, cwd=workspace)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # The console also delivers Ctrl+C to the foreground reader.
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
