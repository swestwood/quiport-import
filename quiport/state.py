"""Resumable progress tracking.

Correctness does not depend on this -- RawStore already makes every call
idempotent. The ledger exists so a resumed run can skip work it has already
decided about, without re-reading the whole cache.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Ledger:
    """Append-only JSONL of completed work units. Streams; never loaded whole."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._done: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        count = 0
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("status") == "done":
                    self._done.setdefault(rec.get("phase", ""), set()).add(str(rec.get("unit")))
                count += 1
        if count:
            log.info("Resuming: %d ledger entries, %d completed units",
                     count, sum(len(v) for v in self._done.values()))

    def is_done(self, phase: str, unit: str) -> bool:
        return str(unit) in self._done.get(phase, ())

    def mark(self, phase: str, unit: str, status: str = "done", **extra) -> None:
        rec = {"phase": phase, "unit": str(unit), "status": status, "ts": _now()}
        rec.update(extra)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            if status == "done":
                self._done.setdefault(phase, set()).add(str(unit))

    def reopen(self, phase: str, units) -> int:
        """Mark units as needing work again, for `audit --repair`.

        Append-only on disk: a `reopened` record is written so the history stays
        readable, and the in-memory done-set is updated so the current process
        picks the units up.
        """
        reopened = 0
        for unit in units:
            if str(unit) in self._done.get(phase, ()):
                self._done[phase].discard(str(unit))
            self.mark(phase, unit, status="reopened")
            reopened += 1
        return reopened

    def count(self, phase: str) -> int:
        return len(self._done.get(phase, ()))

    def summary(self) -> dict:
        return {phase: len(units) for phase, units in sorted(self._done.items())}


class Checkpoint:
    """Small mutable dict of run-level position, written atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                log.warning("Unreadable checkpoint at %s; starting fresh", self.path)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value
        self.data["updated_at"] = _now()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class JsonlFile:
    """Streaming record file for discovery output (folders, threads)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_all(self, records) -> int:
        n = 0
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
                n += 1
        tmp.replace(self.path)
        return n

    def read_all(self):
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue

    def exists(self) -> bool:
        return self.path.exists()
