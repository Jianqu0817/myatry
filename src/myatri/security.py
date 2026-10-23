"""Sensitive-path exclusions shared by backup and restore (not a secret scanner)."""
from pathlib import PurePath


def is_private_path(path) -> bool:
    parts = [part.lower() for part in PurePath(path).parts]
    if not parts:
        return False
    if any(part in {"credentials", ".credentials", "secrets", ".secrets", ".git", ".venv"} for part in parts):
        return True
    name = parts[-1]
    return ((name.startswith(".env") and name != ".env.example") or name.endswith(".local.toml") or
            name in {"credentials.json", "credentials.toml", "credentials.yaml", "credentials.yml",
                     "secrets.json", "secrets.toml", "secrets.yaml", "secrets.yml", "api_key.txt", "api-key.txt"} or
            name.endswith((".credential", ".credential.json", ".pem", ".pfx", ".p12", ".key")))
