"""Small durable runtime settings; updates are atomic and do not rewrite TOML."""
from contextlib import closing
import math
from pathlib import Path
import sqlite3


class SettingsConflict(ValueError):
    pass


class RuntimeSettings:
    def __init__(self, path, probability, recovery_enabled=True):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                "revision INTEGER NOT NULL, probability REAL NOT NULL, recovery_enabled INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,0,?,?)", (probability, int(recovery_enabled)))

    def read(self):
        with closing(sqlite3.connect(self.path)) as db:
            rev, probability, enabled = db.execute("SELECT revision,probability,recovery_enabled FROM settings WHERE id=1").fetchone()
        return {"revision": rev, "random_reply_probability": probability, "recovery_enabled": bool(enabled)}

    def update(self, revision, probability, enabled):
        if (type(revision) is not int or revision < 0 or type(probability) not in (int, float)
                or not math.isfinite(probability) or not 0 <= probability <= 1 or type(enabled) is not bool):
            raise ValueError("invalid runtime settings")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE settings SET revision=revision+1,probability=?,recovery_enabled=? "
                "WHERE id=1 AND revision=?", (probability, int(enabled), revision)).rowcount
            if not changed:
                raise SettingsConflict("runtime settings changed")
        return self.read()
