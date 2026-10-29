"""Online SQLite backups of the labtender service's state, kept for a week."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .db import Store

EVERY_S = 24 * 3600
KEEP = 7


def backup(store: Store, dest: Path) -> Path:
    """Copy the live database to ``dest`` with SQLite's online backup (safe while in use)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial")
    out = sqlite3.connect(tmp)
    try:
        store.conn.backup(out)
    finally:
        out.close()
    tmp.replace(dest)
    return dest


def backup_if_due(store: Store, folder: Path, now: float | None = None) -> Path | None:
    """Back up once a day into ``folder`` and keep the newest ``KEEP`` copies."""
    now = now or time.time()
    existing = sorted(folder.glob("labtender-*.sqlite"))
    if existing and now - existing[-1].stat().st_mtime < EVERY_S:
        return None
    path = backup(
        store, folder / time.strftime("labtender-%Y%m%d-%H%M%S.sqlite", time.localtime(now))
    )
    for old in sorted(folder.glob("labtender-*.sqlite"))[:-KEEP]:
        old.unlink()
    store.audit("labtender", "backup", str(path.name))
    return path
