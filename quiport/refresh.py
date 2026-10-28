"""Re-fetch documents that already exist in an export, and pull in new ones.

Two entry points:

* `refresh_url` -- given a Quip document URL, fetch the current version and
  update it on disk. Handles a document that is new to the export, one that has
  changed since last time, and one that has not changed at all.

* `find_updates` -- ask Quip which threads have been modified since we last saw
  them, and report only the ones whose `updated_usec` actually differs from the
  value recorded in the export.

Every call goes through RawStore as usual, so a refresh is recorded exactly like
the original harvest -- and the response it replaces is archived rather than
overwritten.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import harvest as Hv
from . import quipclient as q
from .model import Thread, unwrap
from .quipid import id_type, is_secret_path

log = logging.getLogger(__name__)

QUIP_URL_RE = re.compile(r"^(?:https?://)?[\w.-]*quip\.com/([\w-]+)", re.I)

NEW, UPDATED, UNCHANGED, FAILED = "new", "updated", "unchanged", "failed"


@dataclass
class RefreshResult:
    token: str
    thread_id: str = ""
    title: str = ""
    status: str = FAILED
    previous_usec: int = 0
    current_usec: int = 0
    detail: str = ""
    calls: int = 0

    @property
    def changed(self) -> bool:
        return self.status in (NEW, UPDATED)


def parse_target(url_or_id: str) -> str:
    """Pull the identifier out of a Quip URL, or accept a bare id."""
    text = (url_or_id or "").strip()
    m = QUIP_URL_RE.match(text)
    if m:
        return m.group(1)
    return text.split("/")[0].strip()


def stored_updated_usec(cfg) -> dict:
    """thread_id -> the `updated_usec` this export last recorded."""
    path = cfg.root / "id-map.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {tid: rec.get("updated_usec") or 0
            for tid, rec in (data.get("threads") or {}).items()}


def stored_aliases(cfg) -> dict:
    path = cfg.root / "id-map.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("aliases") or {}
    except (OSError, ValueError):
        return {}


def resolve_thread_id(client, token: str, aliases: dict) -> str:
    """Secret path or id -> permanent thread id.

    Checks the export's alias index first; a token we have never seen costs one
    API call, which also proves the thread exists and is readable.
    """
    if token in aliases:
        return aliases[token]
    if id_type(token) == "thread":
        return token
    data = q.threads_v2(client, [token]) or {}
    for key, rec in data.items():
        thread = unwrap(rec)
        if thread.get("id"):
            return thread["id"]
    return ""


def refresh_threads(client, cfg, ledger, tokens, force_render=False) -> list:
    """Re-fetch each token. Returns one RefreshResult per token."""
    aliases = stored_aliases(cfg)
    previous = stored_updated_usec(cfg)
    results = []

    for token in tokens:
        before = client.stats["network"]
        result = RefreshResult(token=token)

        thread_id = resolve_thread_id(client, token, aliases)
        if not thread_id:
            result.detail = "could not resolve to a thread (unknown, or no access)"
            results.append(result)
            log.warning("  %s: %s", token, result.detail)
            continue
        result.thread_id = thread_id
        result.previous_usec = previous.get(thread_id, 0)

        try:
            thread = _refetch(client, cfg, thread_id)
        except q.QuipAuthError:
            raise                       # the token, not this document
        except Exception as e:
            # The harvest fetchers now raise when something never answered.
            # One document failing must be reported, not abort the batch --
            # `check-updates` can be refreshing dozens at a time.
            result.detail = f"{type(e).__name__}: {e}"
            results.append(result)
            log.warning("  %s: %s", thread_id, result.detail)
            continue
        if thread is None:
            result.detail = "metadata fetch failed"
            results.append(result)
            continue

        result.title = thread.display_title
        result.current_usec = thread.updated_usec

        if result.previous_usec == 0:
            result.status = NEW
        elif thread.updated_usec != result.previous_usec:
            result.status = UPDATED
        else:
            result.status = UNCHANGED

        # A thread new to the export must also join the discovery list, or the
        # renderer will not know it exists.
        if result.status == NEW:
            _register_thread(cfg, thread_id, thread.folder_ids)

        result.calls = client.stats["network"] - before
        results.append(result)
        log.info("  %s %s (%s, %d calls)", result.status.upper(),
                 result.title[:48] or thread_id, thread_id, result.calls)

    return results


def _refetch(client, cfg, thread_id: str) -> Thread | None:
    """Fetch every per-thread call again, bypassing the cache.

    `allow_fetch` is not enough: the store would serve the cached copy. A
    refresh deliberately goes to the network, and `RawStore.save` archives
    whatever it replaces.
    """
    store = client.store
    original_retry = getattr(store, "retry_errors", False)
    # Temporarily treat every cached entry as stale for this thread.
    store.retry_errors = True
    try:
        forced = _ForceMissing(store, thread_id)
        client.store = forced

        v1 = q.threads_v1(client, [thread_id]) or {}
        v2 = q.threads_v2(client, [thread_id]) or {}
        rec1, rec2 = v1.get(thread_id), v2.get(thread_id)
        if not rec1 and not rec2:
            return None
        # Same rule as harvest: an unreadable thread is *present* with an error
        # key, so absence means the call failed. Carrying on without the V2
        # record would drop `spreadsheet_summary`, skip the workbook, and still
        # report the refresh as successful.
        if rec1 is None or rec2 is None:
            missing = " and ".join(n for n, r in (("V1", rec1), ("V2", rec2))
                                   if r is None)
            raise Hv.Incomplete(f"{missing} bulk metadata did not answer")

        thread = Thread.from_raw(thread_id, rec1, rec2)
        stats = {"threads": 0, "blobs": 0, "blob_errors": 0, "messages": 0,
                 "errors": 0, "xlsx": 0, "xlsx_errors": 0}
        Hv._fetch_content(client, thread, cfg, stats)
        Hv._fetch_messages(client, thread, stats)
        if cfg.sharing == "full":
            Hv._fetch_sharing(client, thread)
        Hv._fetch_blobs(client, thread, cfg, stats)
        Hv._fetch_xlsx(client, thread, stats)

        user_ids = set(thread.expanded_user_ids) | set(thread.member_ids)
        if thread.author_id:
            user_ids.add(thread.author_id)
        for m in thread.messages:
            if m.get("author_id"):
                user_ids.add(m["author_id"])
            user_ids.update(m.get("mention_user_ids") or [])
        user_ids.update(Hv.MENTION_RE.findall(thread.html or ""))
        Hv._fetch_users(client, cfg, {u for u in user_ids if id_type(u) == "user"})
        return thread
    finally:
        client.store = store
        store.retry_errors = original_retry


class _ForceMissing:
    """Store wrapper that reports every entry as absent, so writes still land.

    Only for the duration of one refresh: reads miss (forcing a network call),
    writes go through to the real store, which archives what it supersedes.
    """

    def __init__(self, store, thread_id: str):
        self._store = store
        self._thread_id = thread_id
        self._seen: set = set()

    def load(self, method, endpoint, params):
        key = self._store.key(method, endpoint, params)
        if key in self._seen:                       # already refreshed this run
            return self._store.load(method, endpoint, params)
        self._seen.add(key)
        return None

    def save(self, *args, **kwargs):
        return self._store.save(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _register_thread(cfg, thread_id: str, folder_ids) -> None:
    """Add a thread to state/threads.jsonl if the export has never seen it."""
    from .state import JsonlFile

    f = JsonlFile(cfg.state_dir / "threads.jsonl")
    records = list(f.read_all())
    if any(r.get("id") == thread_id for r in records):
        return
    records.append({"id": thread_id, "folder_ids": list(folder_ids or [])})
    f.write_all(records)
    log.info("  registered %s as a new thread in this export", thread_id)


# -- update detection ----------------------------------------------------

def find_updates(client, cfg, since_usec: int = 0, limit_pages: int = 200) -> dict:
    """Threads whose `updated_usec` differs from what the export recorded.

    Uses `/1/users/current/threads-modified-after-usec`, which returns
    `updated_usec` per thread and covers everything the user can see, not just
    what they own.
    """
    previous = stored_updated_usec(cfg)
    aliases = stored_aliases(cfg)
    known_ids = set(previous)

    changed, new, deleted, seen = [], [], [], 0
    cursor, pages = None, 0
    while pages < limit_pages:
        pages += 1
        resp = client.get(
            "/1/users/current/threads-modified-after-usec",
            last_modified_after_usec=since_usec, threads_meta="true",
            limit=100, cursor=cursor)
        # A failed page is not "no more changes". Breaking here would report a
        # shorter list of modified documents than actually exists, and the
        # operator would approve it believing it complete.
        if not resp.ok:
            raise RuntimeError(
                f"listing modified threads failed on page {pages} "
                f"(HTTP {resp.status}); the list would be incomplete. Re-run.")
        try:
            page = resp.json()
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("listing modified threads returned unreadable JSON")
        if not page:
            break
        threads = page.get("threads") or {}
        for tid, meta in threads.items():
            seen += 1
            current = (meta or {}).get("updated_usec") or 0
            if (meta or {}).get("is_deleted"):
                if tid in known_ids:
                    deleted.append({"thread_id": tid, "link": (meta or {}).get("link", "")})
                continue
            if tid not in known_ids:
                new.append({"thread_id": tid, "updated_usec": current,
                            "link": (meta or {}).get("link", ""),
                            "type": ((meta or {}).get("type") or "").lower()})
            elif current != previous.get(tid, 0):
                changed.append({"thread_id": tid, "updated_usec": current,
                                "previous_usec": previous.get(tid, 0),
                                "link": (meta or {}).get("link", ""),
                                "type": ((meta or {}).get("type") or "").lower()})
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor or not threads:
            break

    log.info("Checked %d threads across %d pages: %d changed, %d new, %d deleted",
             seen, pages, len(changed), len(new), len(deleted))
    return {"checked": seen, "pages": pages, "changed": changed,
            "new": new, "deleted": deleted, "aliases": aliases}


def describe(entry: dict, titles: dict) -> str:
    tid = entry["thread_id"]
    title = titles.get(tid) or entry.get("link") or tid
    return f"{title}  ({tid})"
