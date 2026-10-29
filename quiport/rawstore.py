"""The raw API cache: every request and response, on disk, forever.

Three sibling files per call, named by the sha256 of a canonical request string:

    <sha>.request     plain text -- greppable, no index needed
    <sha>.meta.json   status, all response headers, timing, body hash
    <sha>.body        the exact bytes the server sent

This is the irreplaceable artefact. Rendering reads only from here, so the
export can be rebuilt with better logic long after the API is switched off.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import threading
import urllib.parse
from pathlib import Path

from .quipclient import Response, is_answer, is_expired_cursor

log = logging.getLogger(__name__)

# Path segments that are part of the API's vocabulary rather than an identifier.
# Anything else in a path is assumed to be an id and collapsed to "ID" so that
# directory names stay readable and bounded.
KNOWN_SEGMENTS = frozenset("""
    1 2 threads folders users messages blob websockets oauth
    current recent search new new-chat new-document contacts read-only
    html members invited-members export docx xlsx pdf async copy
    threads-modified-after-usec link-sharing-settings
""".split())

ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")


def endpoint_slug(method: str, endpoint: str) -> str:
    """`/2/threads/Doc9AAxmpl2/html` -> `GET/2_threads_ID_html`."""
    parts = []
    for seg in endpoint.strip("/").split("/"):
        if not seg:
            continue
        if seg in KNOWN_SEGMENTS:
            parts.append(seg)
        elif ID_RE.match(seg):
            parts.append("ID")
        else:
            parts.append(re.sub(r"[^A-Za-z0-9_-]", "_", seg))
    return f"{method.upper()}/" + ("_".join(parts) or "root")


def canonical(method: str, endpoint: str, params: dict | None) -> str:
    """Stable text form of a request. Same logical call -> same string."""
    params = params or {}
    items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
    query = urllib.parse.urlencode(items) if items else ""
    return f"{method.upper()} {endpoint}" + (f"?{query}" if query else "")


class RawStore:
    """Content-addressed request/response cache."""

    def __init__(self, root: Path, retry_errors: bool = False):
        self.root = Path(root)
        self.retry_errors = retry_errors
        self.index_path = self.root / "index.jsonl"
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- addressing ------------------------------------------------------

    @staticmethod
    def key(method: str, endpoint: str, params: dict | None) -> str:
        return hashlib.sha256(canonical(method, endpoint, params).encode("utf-8")).hexdigest()

    def paths(self, method: str, endpoint: str, params: dict | None):
        k = self.key(method, endpoint, params)
        # Shard by hash prefix: a flat directory of millions of files is slow
        # on every filesystem this might land on.
        d = self.root / endpoint_slug(method, endpoint) / k[:2]
        return d, k

    # -- reading ---------------------------------------------------------

    def has(self, method: str, endpoint: str, params: dict | None) -> bool:
        d, k = self.paths(method, endpoint, params)
        return (d / f"{k}.meta.json").exists()

    def load(self, method: str, endpoint: str, params: dict | None) -> Response | None:
        d, k = self.paths(method, endpoint, params)
        meta_path = d / f"{k}.meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("Corrupt meta for %s; refetching", canonical(method, endpoint, params))
            return None

        status = meta.get("status", 0)
        # A cached entry is only usable if the server actually answered. Status 0
        # (never reached the server), 5xx and 429 are failures to obtain an
        # answer; serving one from cache is how a single network blip
        # permanently loses a document. Always re-request those.
        if not is_answer(status):
            log.info("cached non-answer (HTTP %s) for %s; will re-request",
                     status, canonical(method, endpoint, params))
            return None
        # 4xx answers are real but may reflect access that has since widened.
        if self.retry_errors and not (200 <= status < 300):
            return None

        body_path = d / f"{k}.body"
        body = body_path.read_bytes() if body_path.exists() else b""

        # "invalid or expired cursor" arrives as a 400, which otherwise looks
        # like a permanent answer. Serving it back makes the chain unfinishable
        # forever, so it is always re-requested -- see `is_expired_cursor`.
        if is_expired_cursor(status, body):
            log.info("cached expired-cursor 400 for %s; will re-request",
                     canonical(method, endpoint, params))
            return None

        return Response(status, meta.get("headers", {}), body, endpoint,
                        meta.get("params", {}), from_cache=True)

    # -- writing ---------------------------------------------------------

    def save(self, method, endpoint, params, status, headers, body, elapsed_ms=None) -> None:
        params = params or {}
        d, k = self.paths(method, endpoint, params)
        d.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        canon = canonical(method, endpoint, params)

        # A failure must never displace a success. Without this, re-fetching a
        # document during a network outage would archive the good response and
        # leave the canonical name holding a 500 -- content still on disk, but
        # invisible to `load`. The failure is recorded beside it for the audit.
        if not is_answer(status) and self._cached_status(d, k) is not None \
                and 200 <= self._cached_status(d, k) < 300:
            self._record_failed_attempt(d, k, status, headers, body, canon, now)
            log.warning("keeping cached success for %s; this attempt returned %s",
                        canon, status)
            return

        # Re-fetching the same request must not destroy what it replaces. The
        # newest response stays at the canonical name so `load` remains simple;
        # the previous one is filed under `superseded/` with the timestamp it
        # was originally fetched at.
        self._supersede(d, k, body)

        (d / f"{k}.body").write_bytes(body or b"")

        # Plain text so `grep -r "thread_id = XYZ" raw/` just works.
        lines = [
            f"{method.upper()} {endpoint}",
            f"endpoint = {endpoint}",
        ]
        for pk, pv in sorted(params.items()):
            lines.append(f"{pk} = {pv}")
        lines += [
            f"canonical = {canon}",
            f"status = {status}",
            f"fetched_at = {now}",
            f"body_bytes = {len(body or b'')}",
            "",
        ]
        (d / f"{k}.request").write_text("\n".join(lines), encoding="utf-8")

        meta = {
            "canonical": canon, "method": method.upper(), "endpoint": endpoint,
            "params": params, "status": status, "headers": dict(headers or {}),
            "fetched_at": now, "elapsed_ms": elapsed_ms,
            "body_file": f"{k}.body", "body_bytes": len(body or b""),
            "body_sha256": hashlib.sha256(body or b"").hexdigest(),
        }
        (d / f"{k}.meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

        with self._lock:
            with self.index_path.open("a", encoding="utf-8") as f:
                # A previous append torn by power loss leaves the file without a
                # trailing newline. Writing straight after it fuses this entry
                # onto the fragment, so *this* response -- a perfectly good one --
                # becomes unparseable and invisible to the renderer. Damage must
                # not propagate past the line that was actually interrupted.
                if self._index_needs_newline():
                    f.write("\n")
                f.write(json.dumps({
                    "key": k, "canonical": canon, "status": status,
                    "bytes": len(body or b""), "fetched_at": now,
                    "path": str((d / f"{k}.body").relative_to(self.root)),
                }) + "\n")

    # -- inspection ------------------------------------------------------

    def iter_index(self):
        if not self.index_path.exists():
            return
        with self.index_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue

    def _index_needs_newline(self) -> bool:
        """True if the index exists and does not end in a newline."""
        try:
            if not self.index_path.exists() or self.index_path.stat().st_size == 0:
                return False
            with self.index_path.open("rb") as f:
                f.seek(-1, 2)
                return f.read(1) != b"\n"
        except OSError:
            return False

    def _cached_status(self, d: Path, k: str) -> int | None:
        meta_path = d / f"{k}.meta.json"
        if not meta_path.exists():
            return None
        try:
            return int(json.loads(meta_path.read_text(encoding="utf-8")).get("status", 0))
        except (OSError, ValueError, TypeError):
            return None

    def _record_failed_attempt(self, d: Path, k: str, status, headers, body,
                               canon: str, now: str) -> None:
        """Keep a failed re-request for the record, out of the way of `load`."""
        archive = d / "failed"
        archive.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9A-Za-z]", "", now)
        (archive / f"{k}.{stamp}.meta.json").write_text(json.dumps({
            "canonical": canon, "status": status, "headers": dict(headers or {}),
            "attempted_at": now, "body_bytes": len(body or b""),
            "note": "re-request failed; the cached success was kept",
        }, indent=1), encoding="utf-8")
        if body:
            (archive / f"{k}.{stamp}.body").write_bytes(body)

    def failed_attempt_count(self) -> int:
        return sum(1 for _ in self.root.rglob("failed/*.meta.json"))

    def _supersede(self, d: Path, k: str, new_body: bytes) -> None:
        """Archive an existing response before overwriting it.

        Only when the bytes actually differ -- re-fetching an unchanged
        document should not litter the store with identical copies.
        """
        meta_path = d / f"{k}.meta.json"
        body_path = d / f"{k}.body"
        if not meta_path.exists():
            return
        try:
            old_body = body_path.read_bytes() if body_path.exists() else b""
            if old_body == (new_body or b""):
                return
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        stamp = re.sub(r"[^0-9A-Za-z]", "", meta.get("fetched_at", "") or "old")
        archive = d / "superseded"
        archive.mkdir(parents=True, exist_ok=True)
        for suffix in (".body", ".meta.json", ".request"):
            src = d / f"{k}{suffix}"
            if src.exists():
                src.replace(archive / f"{k}.{stamp}{suffix}")
        log.debug("Superseded %s (kept as %s.%s)", k, k, stamp)

    def superseded_count(self) -> int:
        return sum(1 for _ in self.root.rglob("superseded/*.body"))

    def fetched_at(self, method: str, endpoint: str, params: dict | None = None) -> str:
        """When this call was actually made, as stored ISO-8601. '' if unknown."""
        d, k = self.paths(method, endpoint, params or {})
        meta_path = d / f"{k}.meta.json"
        if not meta_path.exists():
            return ""
        try:
            return json.loads(meta_path.read_text(encoding="utf-8")).get("fetched_at", "")
        except (ValueError, OSError):
            return ""

    def cursor_chain(self, endpoint: str, params: dict | None = None) -> dict:
        """Follow `response_metadata.next_cursor` through the cache.

        The one place that knows what a *complete* paginated result looks like: a
        chain is complete only when the last cached page carries an empty cursor.
        Anything else -- a page missing, an unreadable body, a chain still
        pointing forward -- means what is on disk is a prefix of the real answer.

        That distinction is the whole point. Returning the partial data and
        letting the caller assume it was everything is how an interrupted harvest
        silently exports 40% of a document, and it is invisible to `verify`,
        whose comparison reads from this same cache.

        Returns {"pages": [parsed...], "complete": bool, "reason": str}.
        """
        pages: list = []
        base = dict(params or {})
        cursor, guard = None, 0
        while guard < 10_000:
            guard += 1
            page_params = dict(base)
            if cursor:
                page_params["cursor"] = cursor
            resp = self.load("GET", endpoint, page_params)
            if resp is None:
                return {"pages": pages, "complete": False, "status": 0,
                        "reason": ("never fetched" if not pages else
                                   f"page {len(pages) + 1} missing from cache")}
            if not resp.ok:
                # `status` lets the caller tell "the server refused" (a permanent
                # answer) from "we never got it" (retryable).
                #
                # A failure on a *continuation* page is never permanent, whatever
                # the code. A cursor is a temporary handle: recorded before an
                # interruption and replayed hours later it can be rejected with
                # 400, which looks like a definitive answer and is not -- the
                # content is still there behind a fresh cursor. Reporting status 0
                # for those keeps them in the retryable bucket, which is what
                # makes a resumed harvest finish the document instead of
                # declaring it done.
                stale_cursor = len(pages) > 0
                return {"pages": pages, "complete": False,
                        "status": 0 if stale_cursor else resp.status,
                        "stale_cursor": stale_cursor,
                        "reason": (f"page {len(pages) + 1} returned HTTP "
                                   f"{resp.status}"
                                   + (" (cursor no longer valid; the chain must be "
                                      "re-walked from the first page)"
                                      if stale_cursor else ""))}
            try:
                data = json.loads(resp.body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {"pages": pages, "complete": False, "status": resp.status,
                        "reason": f"page {len(pages) + 1} is not readable JSON"}
            pages.append(data)
            cursor = ((data.get("response_metadata") or {}).get("next_cursor")
                      if isinstance(data, dict) else None)
            if not cursor:
                return {"pages": pages, "complete": True, "status": 200, "reason": ""}
        return {"pages": pages, "complete": False, "status": 200,
                "reason": "cursor chain did not terminate"}

    def html_chain(self, thread_id: str) -> dict:
        """A thread's HTML, plus whether the cursor chain actually ended."""
        chain = self.cursor_chain(f"/2/threads/{thread_id}/html")
        html = "".join(p.get("html", "") for p in chain["pages"]
                       if isinstance(p, dict))
        reason = chain["reason"]
        if reason == "never fetched":
            reason = "no HTML fetched"
        return {"html": html, "pages": len(chain["pages"]),
                "complete": chain["complete"], "status": chain.get("status", 0),
                "reason": reason}

    def load_thread_html(self, thread_id: str) -> str:
        """The HTML as cached, complete or not. Prefer `html_chain`."""
        return self.html_chain(thread_id)["html"]

    def message_chain(self, thread_id: str, page: int = 100) -> dict:
        """Walk the backwards-paged message chain and say whether it ended.

        Harvest stops when a page comes back shorter than `page`, so that short
        final page is the termination marker. A thread with no messages at all is
        complete: the first call answers with an empty list.
        """
        seen, pages, max_created, guard = set(), 0, None, 0
        while guard < 10_000:
            guard += 1
            params = {"count": page}
            if max_created is not None:
                params["max_created_usec"] = max_created
            resp = self.load("GET", f"/1/messages/{thread_id}", params)
            if resp is None:
                return {"messages": len(seen), "pages": pages, "complete": False,
                        "status": 0,
                        "reason": ("messages never fetched" if pages == 0 else
                                   f"page {pages + 1} missing from cache")}
            if not resp.ok:
                # As in cursor_chain: a failure past the first page means the
                # paging handle went stale, not that the resource is unreadable.
                stale = pages > 0
                return {"messages": len(seen), "pages": pages, "complete": False,
                        "status": 0 if stale else resp.status,
                        "reason": f"page {pages + 1} returned HTTP {resp.status}"
                                  + (" (paging position no longer valid)" if stale else "")}
            try:
                chunk = json.loads(resp.body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {"messages": len(seen), "pages": pages, "complete": False,
                        "status": resp.status,
                        "reason": f"page {pages + 1} is not readable JSON"}
            if not isinstance(chunk, list):
                return {"messages": len(seen), "pages": pages, "complete": False,
                        "status": resp.status,
                        "reason": "message page is not a list"}
            pages += 1
            fresh = [m for m in chunk if m.get("id") not in seen]
            seen.update(m.get("id") for m in fresh)
            if len(chunk) < page or not fresh:
                return {"messages": len(seen), "pages": pages, "complete": True,
                        "status": 200, "reason": ""}
            stamps = [m.get("created_usec") for m in chunk if m.get("created_usec")]
            if not stamps:
                return {"messages": len(seen), "pages": pages, "complete": True,
                        "status": 200, "reason": ""}
            nxt = min(stamps) - 1
            if max_created is not None and nxt >= max_created:
                return {"messages": len(seen), "pages": pages, "complete": True,
                        "status": 200, "reason": ""}
            max_created = nxt
        return {"messages": len(seen), "pages": pages, "complete": False,
                "status": 200, "reason": "message chain did not terminate"}

    def reindex(self) -> dict:
        """Rebuild `index.jsonl` from the files on disk.

        The index is an append-only convenience, and the renderer reads bulk
        metadata and user names through it -- so a line lost to an interrupted
        append silently removes threads from the export while their responses sit
        on disk, complete. This makes the files authoritative again.

        Written to a temporary file and moved into place, so an interrupted
        reindex cannot leave a half-index behind.
        """
        rebuilt, skipped = 0, 0
        tmp = self.index_path.with_suffix(".rebuilding")
        with tmp.open("w", encoding="utf-8") as f:
            for meta_path in sorted(self.root.rglob("*.meta.json")):
                if "superseded" in meta_path.parts or "failed" in meta_path.parts:
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    skipped += 1
                    continue
                key = meta_path.name[: -len(".meta.json")]
                body = meta_path.with_name(meta.get("body_file") or f"{key}.body")
                f.write(json.dumps({
                    "key": key, "canonical": meta.get("canonical", ""),
                    "status": meta.get("status", 0),
                    "bytes": meta.get("body_bytes", 0),
                    "fetched_at": meta.get("fetched_at", ""),
                    "path": str(body.relative_to(self.root)),
                }) + "\n")
                rebuilt += 1
        tmp.replace(self.index_path)
        log.info("Rebuilt index with %d entries (%d unreadable)", rebuilt, skipped)
        return {"entries": rebuilt, "unreadable": skipped}

    def stats(self) -> dict:
        total = ok = errors = size = 0
        for rec in self.iter_index():
            total += 1
            size += rec.get("bytes", 0)
            if 200 <= rec.get("status", 0) < 300:
                ok += 1
            else:
                errors += 1
        return {"calls": total, "ok": ok, "errors": errors, "bytes": size}
