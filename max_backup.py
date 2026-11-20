"""Consistent SQLite backups (online, via the sqlite3 backup API).

Files live in <MAX_BACKUP_DIR | <db dir>/backups>/max-YYYYmmdd-HHMMSS[-label].db.
A daily automatic backup (at/after 03:00) keeps the newest N files.
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from max_auth import get_setting

NAME_RE = re.compile(r"max-\d{8}-\d{6}(?:-[a-z]+)?\.db")
DEFAULTS = {"auto": True, "keep": 14}


def backup_dir(db_path):
    d = Path(os.environ.get("MAX_BACKUP_DIR") or Path(db_path).resolve().parent / "backups")
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings(db_path):
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(get_setting(db_path, "backup", "{}")))
    except ValueError:
        pass
    return cfg


def create(db_path, label=""):
    name = "max-%s%s.db" % (datetime.now().strftime("%Y%m%d-%H%M%S"),
                            "-" + label if label else "")
    dest = backup_dir(db_path) / name
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        os.chmod(dest, 0o600)     # contains password hashes and the MQTT password
    except OSError:
        pass
    return name


def listing(db_path):
    out = []
    for f in sorted(backup_dir(db_path).glob("max-*.db"), reverse=True):
        if NAME_RE.fullmatch(f.name):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size,
                        "created": datetime.fromtimestamp(st.st_mtime).isoformat(
                            timespec="seconds"),
                        "auto": not f.name[:-3].endswith(("-manual", "-vorher"))
                        and f.name.count("-") == 2})
    return out


def path_of(db_path, name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("ungültiger Dateiname")
    p = backup_dir(db_path) / name
    if not p.is_file():
        raise ValueError("Backup nicht gefunden")
    return p


def delete(db_path, name):
    path_of(db_path, name).unlink()


def prune(db_path, keep):
    autos = [b for b in listing(db_path) if b["auto"]]
    for b in autos[keep:]:
        try:
            (backup_dir(db_path) / b["name"]).unlink()
        except OSError:
            pass


def restore(db_path, name):
    """Replace the live database with a backup (a safety copy is made first)."""
    src_path = path_of(db_path, name)
    probe = sqlite3.connect(str(src_path))
    try:
        ok = probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        has = probe.execute("SELECT 1 FROM sqlite_master WHERE name='telegrams'"
                            ).fetchone()
    finally:
        probe.close()
    if not (ok and has):
        raise ValueError("Backup ist beschädigt oder keine MAX!-Datenbank")
    create(db_path, "vorher")
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(db_path, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


class AutoBackup(threading.Thread):
    def __init__(self, db_path):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.last_error = ""

    def run(self):
        while True:
            time.sleep(60)
            try:
                cfg = settings(self.db_path)
                if not cfg["auto"] or datetime.now().hour < 3:
                    continue
                today = datetime.now().strftime("%Y%m%d")
                if not any(b["name"].startswith("max-" + today) and b["auto"]
                           for b in listing(self.db_path)):
                    create(self.db_path)
                    prune(self.db_path, max(1, int(cfg["keep"])))
                self.last_error = ""
            except Exception as e:           # keep the thread alive
                self.last_error = str(e)
