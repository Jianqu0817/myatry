from pathlib import Path


def load_prompt(role: str, version: str = "v1") -> str:
    if role not in {"experience", "association", "theme", "recall", "reflection", "temporary_state", "persona_revision", "reviewer", "compaction", "activation", "attribution", "emotion"}:
        raise ValueError("unknown prompt role")
    import re
    if not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError("invalid prompt version")
    paths = [Path(__file__).parent / role / f"{version}.md", Path(__file__).resolve().parents[3] / "prompts" / role / f"{version}.md"]
    for path in paths:
        if path.exists():
            return path.read_text(encoding="utf-8-sig")
    raise FileNotFoundError(f"missing versioned prompt for {role}")
