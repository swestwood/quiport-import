"""Phase 3 -- build the export tree from the raw cache.

Reads only from RawStore. `allow_fetch` is False by default, so an accidental
network dependency fails loudly instead of quietly making the render
un-reproducible.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from .. import htmlutil as H
from ..config import CONTENT_DIR_NAME
from ..model import Folder, Thread, format_export_time, unwrap
from ..naming import PathAllocator
from ..state import JsonlFile
from .blobs import BlobStore
from .chat import render_chat
from .document import render_document, render_stub
from .folder import render_folder
from .templates import (HIGHLIGHT_NAME, STYLESHEET, STYLESHEET_NAME,
                        UTILS_NAME)

log = logging.getLogger(__name__)

THREAD_BATCH = 25


class RenderContext:
    """Shared lookups and formatting for all renderers."""

    def __init__(self, cfg, raw_store):
        self.cfg = cfg
        self.raw = raw_store
        self.content_dir_name = CONTENT_DIR_NAME
        self.blobs = BlobStore(cfg, raw_store, CONTENT_DIR_NAME)
        self.users: dict = {}
        self.folders: dict[str, Folder] = {}
        self._fetched_cache: dict[str, str] = {}
        # Fallback for pages that belong to no single API call.
        self.harvested_at = ""

    def exported_at(self, thread_id: str = "") -> str:
        """When the API call behind this page was actually made.

        Not render time: the export claims to be a snapshot of Quip at a moment,
        and that moment is when the data was fetched.
        """
        if thread_id and thread_id in self._fetched_cache:
            return self._fetched_cache[thread_id]
        iso = ""
        if thread_id:
            iso = self.raw.fetched_at("GET", f"/2/threads/{thread_id}/html", {})
        if not iso:
            iso = self.harvested_at
        formatted = format_export_time(iso)
        if thread_id:
            self._fetched_cache[thread_id] = formatted
        return formatted

    def user_name(self, user_id: str) -> str:
        if not user_id:
            return ""
        u = self.users.get(user_id)
        if isinstance(u, dict):
            return u.get("name") or user_id
        return user_id

    def describe_sharing(self, thread: Thread) -> str:
        """"Private" or "Shared with A, B" -- the spec's metadata line.

        Built from V1's `expanded_user_ids`, which is the only field that
        includes access inherited through folders; V2's /members endpoint
        documents that it excludes folder-, link- and email-based access.
        """
        ids = [u for u in dict.fromkeys(thread.expanded_user_ids + thread.member_ids) if u]
        names = [self.user_name(u) for u in ids]
        names = [n for n in names if n]

        company = (thread.sharing or {}).get("company_mode")
        bits = []
        if len(names) <= 1:
            bits.append("Private")
        else:
            shown = names[:12]
            more = len(names) - len(shown)
            label = ", ".join(H.escape_text(n) for n in shown)
            if more > 0:
                label += f" and {more} more"
            bits.append(f"Shared with {label}")
        if company:
            bits.append(f"company link: {H.escape_text(str(company).lower())}")
        if thread.invited_emails:
            bits.append("invited by email: " +
                        ", ".join(H.escape_text(e) for e in thread.invited_emails[:6]))
        return " &middot; ".join(bits)

    def describe_folder_sharing(self, folder: Folder) -> str:
        names = [self.user_name(u) for u in folder.member_ids]
        names = [n for n in names if n]
        if folder.is_private and len(names) <= 1:
            return "Private"
        if not names:
            return "Private" if folder.is_private else "Shared"
        shown = names[:12]
        more = len(names) - len(shown)
        label = ", ".join(H.escape_text(n) for n in shown)
        if more > 0:
            label += f" and {more} more"
        return f"Shared with {label}"


def load_all(cfg, raw_store, allow_fetch=False):
    """Rebuild folders and threads from the cache."""
    ctx = RenderContext(cfg, raw_store)

    folder_records = list(JsonlFile(cfg.state_dir / "folders.jsonl").read_all())
    thread_records = list(JsonlFile(cfg.state_dir / "threads.jsonl").read_all())
    if not thread_records:
        raise RuntimeError("Nothing to render: run discover and harvest first")

    # Exports discovered before folder colour was recorded have no `color` in
    # folders.jsonl. The cached `/1/folders/` responses still carry it, so read
    # it from there rather than making the operator re-run discovery.
    folder_colors = _folder_colors_from_cache(raw_store)

    for rec in folder_records:
        f = Folder(
            id=rec["id"], title=rec.get("title", ""),
            folder_type=rec.get("folder_type", ""),
            color=(rec.get("color") or folder_colors.get(rec["id"], "")),
            created_usec=rec.get("created_usec", 0),
            updated_usec=rec.get("updated_usec", 0),
            link=rec.get("link", ""), creator_id=rec.get("creator_id", ""),
            member_ids=rec.get("member_ids") or [],
            child_thread_ids=rec.get("child_thread_ids") or [],
            child_folder_ids=rec.get("child_folder_ids") or [],
            restricted_child_ids=rec.get("restricted_child_ids") or [],
            parent_id=rec.get("parent_id", ""),
        )
        ctx.folders[f.id] = f

    threads = _load_threads(cfg, raw_store, [r["id"] for r in thread_records])
    for rec in thread_records:
        t = threads.get(rec["id"])
        if t:
            for fid in rec.get("folder_ids") or []:
                if fid not in t.folder_ids:
                    t.folder_ids.append(fid)

    ctx.users = _load_users(raw_store, threads)
    # Fallback timestamp: the most recent call in the raw cache.
    newest = ""
    for rec in raw_store.iter_index():
        stamp = rec.get("fetched_at", "")
        if stamp > newest:
            newest = stamp
    ctx.harvested_at = newest
    return ctx, threads


def _folder_colors_from_cache(raw) -> dict:
    """folder id -> Quip colour name, read from the cached `/1/folders/` calls.

    Same reasoning as `_load_bulk_metadata`: scan the append-only index and
    merge, rather than reconstructing `?ids=` batch keys. Only needed for
    exports discovered before `folders.jsonl` carried the colour.
    """
    colors: dict = {}
    if raw is None:
        return colors
    for rec in raw.iter_index():
        if not (200 <= rec.get("status", 0) < 300):
            continue
        if not rec.get("canonical", "").startswith("GET /1/folders/?ids="):
            continue
        path = raw.root / rec.get("path", "")
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for fid, record in data.items():
            color = ((record or {}).get("folder") or {}).get("color")
            if color:
                colors[fid] = color.lower()
    return colors


def _load_bulk_metadata(raw) -> tuple[dict, dict]:
    """Merge every cached bulk-metadata response into per-thread lookups.

    Deliberately *not* reconstructed by re-batching the thread list: batch
    composition depends on how many threads existed when harvest ran, so adding
    a single thread later shifts every boundary and each reconstructed `?ids=`
    key misses the cache. That silently dropped ~11 documents from a rebuild
    after `refresh` added one.

    Scanning the index instead makes the render independent of how the harvest
    happened to group its calls. Index order is append-only, so a superseding
    response naturally wins.
    """
    v1, v2 = {}, {}
    for rec in raw.iter_index():
        if not (200 <= rec.get("status", 0) < 300):
            continue
        canon = rec.get("canonical", "")
        if canon.startswith("GET /1/threads/?ids="):
            target = v1
        elif canon.startswith("GET /2/threads/?ids="):
            target = v2
        else:
            continue
        path = raw.root / rec.get("path", "")
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            target.update(data)
    return v1, v2


def _load_threads(cfg, raw, thread_ids) -> dict:
    """Reassemble each thread from its cached V1/V2 records, HTML and messages."""
    v1, v2 = _load_bulk_metadata(raw)

    threads = {}
    for tid in thread_ids:
        rec1, rec2 = v1.get(tid), v2.get(tid)
        if not rec1 and not rec2:
            continue
        if isinstance(rec1, dict) and "error" in rec1 and "thread" not in rec1:
            log.warning("thread %s unavailable: %s", tid, rec1.get("error"))
            continue
        t = Thread.from_raw(tid, rec1, rec2)
        if t.is_deleted:
            continue
        # A partial cursor chain must be visible, not absorbed: the page would
        # otherwise render a prefix of the document and look perfectly fine.
        chain = raw.html_chain(tid)
        t.html = chain["html"]
        t.html_complete = chain["complete"] or t.is_chat
        t.html_pages = chain["pages"]
        if not t.html_complete and (chain["html"] or not t.is_chat):
            t.errors.append(f"incomplete HTML: {chain['reason']}")
            log.warning("thread %s (%s): incomplete HTML -- %s",
                        tid, t.display_title[:40], chain["reason"])
        t.messages = _load_messages(raw, tid)
        threads[tid] = t
    return threads


def _load_messages(raw, thread_id: str) -> list:
    out, seen, max_created, guard = [], set(), None, 0
    while guard < 1000:
        guard += 1
        params = {"count": 100}
        if max_created is not None:
            params["max_created_usec"] = max_created
        resp = raw.load("GET", f"/1/messages/{thread_id}", params)
        if resp is None or not resp.ok:
            break
        try:
            chunk = resp.json()
        except ValueError:
            break
        if not isinstance(chunk, list) or not chunk:
            break
        fresh = [m for m in chunk if m.get("id") not in seen]
        if not fresh:
            break
        seen.update(m.get("id") for m in fresh)
        out.extend(fresh)
        stamps = [m.get("created_usec") for m in chunk if m.get("created_usec")]
        if not stamps or len(chunk) < 100:
            break
        nxt = min(stamps) - 1
        if max_created is not None and nxt >= max_created:
            break
        max_created = nxt
    out = [m for m in out if m.get("visible", True) is not False]
    out.sort(key=lambda m: m.get("created_usec") or 0)
    return out


def _load_users(raw, threads) -> dict:
    """Users were fetched in batches; recover them from the index."""
    users = {}
    for rec in raw.iter_index():
        if not rec.get("canonical", "").startswith("GET /1/users/"):
            continue
        if 200 <= rec.get("status", 0) < 300:
            path = raw.root / rec["path"]
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(data, dict):
                for uid, u in data.items():
                    if isinstance(u, dict) and u.get("name"):
                        users[uid] = u
    return users


def build_tree(ctx, threads) -> tuple[PathAllocator, list]:
    """Assign one canonical path per thread, plus stub locations."""
    alloc = PathAllocator(ctx.content_dir_name)

    roots = [f for f in ctx.folders.values() if not f.parent_id]
    ordered: list[Folder] = []

    def walk(folder: Folder):
        alloc.allocate_folder(folder.id, folder.display_title, folder.parent_id or None)
        folder.output_path = alloc.folder_paths[folder.id]
        ordered.append(folder)
        for child_id in folder.child_folder_ids:
            child = ctx.folders.get(child_id)
            if child is not None and not child.output_path:
                child.parent_id = folder.id
                walk(child)

    for root in roots:
        walk(root)
    for folder in ctx.folders.values():          # anything unreachable from a root
        if not folder.output_path:
            walk(folder)

    # Membership is recorded from both ends and the two can disagree. A folder's
    # `child_thread_ids` is a snapshot from discovery, so a thread added later by
    # `refresh` is absent from it even though the thread itself knows which
    # folder it is in. Trusting only the folder side silently orphans that
    # thread: it lands at the content root and vanishes from the index tree.
    #
    # Merge the thread side in, so placement, folder index pages and the tree
    # all see the same membership. In-memory only -- folders.jsonl is untouched.
    for t in threads.values():
        for fid in t.folder_ids:
            folder = ctx.folders.get(fid)
            if folder is not None and t.id not in folder.child_thread_ids:
                folder.child_thread_ids.append(t.id)
                log.info("thread %s (%s) claims folder %s that did not list it",
                         t.id, t.display_title[:40], fid)

    # Deterministic: threads claim paths in folder order, then id order.
    for folder in ordered:
        for tid in sorted(folder.child_thread_ids):
            t = threads.get(tid)
            if t is None:
                continue
            path, canonical = alloc.allocate_thread(
                t.id, t.secret_path, t.display_title, folder.id)
            if canonical:
                t.output_path = path
            if folder.output_path not in t.folder_paths:
                t.folder_paths.append(folder.output_path)

    # Threads in no folder at all still need somewhere to live.
    for t in threads.values():
        if not t.output_path:
            path, _ = alloc.allocate_thread(t.id, t.secret_path, t.display_title, None)
            t.output_path = path
    return alloc, ordered


def render_all(cfg, raw_store, ledger=None, progress=None, write_pages=True) -> dict:
    """Build the tree and, unless `write_pages` is False, write it to disk.

    `write_pages=False` is how the reports phase gets the same model objects
    without clobbering the link pass, which runs between the two and rewrites
    every document in place.
    """
    ctx, threads = load_all(cfg, raw_store)
    alloc, folders_ordered = build_tree(ctx, threads)

    if write_pages:
        _write_assets(cfg)

    reports, written, failed = [], 0, 0
    produced: set[str] = set()      # every page this run is responsible for
    for i, t in enumerate(threads.values(), 1):
        # Claimed before the attempt, not after: this run owns that path whether
        # or not rendering succeeds. Adding it afterwards meant a render failure
        # left the path unclaimed and the pruner deleted the previously-good page.
        produced.add(t.output_path)
        try:
            html, report = (render_chat(t, ctx) if t.is_chat else render_document(t, ctx))
            if write_pages:
                out = cfg.root / t.output_path
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(html, encoding="utf-8")
            reports.append((t, report))
            written += 1
        except Exception as e:
            failed += 1
            log.exception("render failed for %s (%s)", t.id, t.display_title[:40])
            if ledger:
                ledger.mark("render", t.id, status="error", error=str(e)[:200])
        if progress and i % 25 == 0:
            progress(i, len(threads))

    # Stubs for the other folders a thread appears in.
    stubs = 0
    for tid, paths in alloc.stubs.items():
        t = threads.get(tid)
        if not t:
            continue
        for stub_path in paths:
            produced.add(stub_path)
            if write_pages:
                out = cfg.root / stub_path
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_stub(t, stub_path, t.output_path, ctx), encoding="utf-8")
            stubs += 1

    # Folder index pages.
    for folder in folders_ordered:
        entries = []
        for child_id in folder.child_folder_ids:
            child = ctx.folders.get(child_id)
            if child and child.output_path:
                entries.append({"title": child.display_title,
                                "path": f"{child.output_path}/index.html",
                                "kind": "folder", "color": child.color,
                                "updated_usec": child.updated_usec})
        for tid in folder.child_thread_ids:
            t = threads.get(tid)
            if not t:
                continue
            path = t.output_path
            for stub in alloc.stubs.get(tid, []):
                if stub.startswith(folder.output_path + "/"):
                    path = stub
                    break
            entries.append({"title": t.display_title, "path": path,
                            "kind": t.type, "updated_usec": t.updated_usec})
        entries.sort(key=lambda e: (e["kind"] != "folder", e["title"].lower()))
        produced.add(f"{folder.output_path}/index.html")
        if write_pages:
            out = cfg.root / folder.output_path / "index.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_folder(folder, entries, ctx), encoding="utf-8")

    pruned = 0
    if write_pages:
        # Deleting on the strength of a run that partly failed risks removing
        # good pages the failure prevented us from regenerating.
        if failed:
            log.warning("%d documents failed to render; skipping stale-page pruning",
                        failed)
        else:
            pruned = _prune_stale_pages(cfg, produced)
        _write_id_map(cfg, threads, ctx)

    stats = {
        "documents": written, "stubs": stubs, "folders": len(folders_ordered),
        "blobs_written": ctx.blobs.count, "blob_bytes": ctx.blobs.bytes,
        "blobs_missing": len(ctx.blobs.missing), "stale_pages_removed": pruned,
        "xlsx_written": ctx.blobs.xlsx_count, "render_failures": failed,
        "incomplete_html": sum(1 for t in threads.values() if not t.html_complete),
    }
    log.info("Rendered %s", stats)
    return {"stats": stats, "ctx": ctx, "threads": threads,
            "reports": reports, "alloc": alloc, "folders": folders_ordered}


def _prune_stale_pages(cfg, produced: set) -> int:
    """Delete pages under the content directory this render did not produce.

    A document's path encodes its folder, title and secret path, so any of those
    changing gives it a new filename -- and without this the old file stays on
    disk forever. It is not merely clutter: it is a stale duplicate that still
    looks like a real document to anyone who opens it, and it made `verify`'s
    id_map check fail when two threads moved out of the content root.

    Only `.html` directly under the content directory is considered. `_files/`
    holds blobs and is never touched, and everything here is reproducible from
    the raw cache anyway.
    """
    removed = 0
    if not cfg.content_dir.exists():
        return 0
    for path in sorted(cfg.content_dir.rglob("*.html")):
        rel = path.relative_to(cfg.root).as_posix()
        if rel in produced or "/_files/" in f"/{rel}":
            continue
        try:
            path.unlink()
            removed += 1
            log.info("removed stale page %s", rel)
        except OSError as e:
            log.warning("could not remove stale page %s: %s", rel, e)

    # A folder that disappeared leaves an empty directory behind it.
    for path in sorted(cfg.content_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_dir() and path.name != "_files" and "_files" not in path.parts:
            try:
                path.rmdir()                     # only succeeds when empty
                log.info("removed empty folder %s", path.relative_to(cfg.root))
            except OSError:
                pass
    return removed


def _write_assets(cfg) -> None:
    """Stylesheet, browser helpers and icons.

    Copied into the export rather than referenced, so the archive stays
    self-contained and works from a file:// URL with no server.
    """
    import shutil

    (cfg.root / STYLESHEET_NAME).write_text(STYLESHEET, encoding="utf-8")

    assets = Path(__file__).resolve().parent.parent / "assets"
    for name in (UTILS_NAME, HIGHLIGHT_NAME):
        source = assets / name
        if source.exists():
            shutil.copyfile(source, cfg.root / name)

    # Icons are inlined into each page as an SVG <symbol> sprite rather than
    # copied: they carry no xmlns, so <img src> would render nothing.


def _write_id_map(cfg, threads, ctx) -> None:
    """Many-to-one index: every id or secret path ever seen -> local file.

    Secret paths can be regenerated by users, so documents may contain links
    carrying paths that are no longer current. Indexing every one we observe --
    not just the current one -- is what makes the future link-resolution tool
    able to follow old links.
    """
    by_thread, aliases = {}, {}
    for t in threads.values():
        by_thread[t.id] = {
            "path": t.output_path, "title": t.display_title,
            "secret_path": t.secret_path, "type": t.type, "link": t.link,
            "folders": t.folder_paths,
            "created_usec": t.created_usec, "updated_usec": t.updated_usec,
        }
        aliases[t.id] = t.id
        if t.secret_path:
            aliases[t.secret_path] = t.id
        if t.link:
            tail = t.link.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                aliases[tail] = t.id
        # Any Quip link inside the document may carry an older secret path.
        for match in _iter_quip_ids(t.html):
            aliases.setdefault(match, aliases.get(match, ""))

    folders = {f.id: {"path": f"{f.output_path}/index.html", "title": f.display_title}
               for f in ctx.folders.values() if f.output_path}
    for f in ctx.folders.values():
        if f.secret_path and f.output_path:
            aliases[f.secret_path] = f.id

    payload = {"threads": by_thread, "folders": folders,
               "aliases": {k: v for k, v in aliases.items() if v}}
    (cfg.root / "id-map.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _iter_quip_ids(html: str):
    import re
    for m in re.finditer(r"https?://[\w.-]*quip\.com/([\w-]{8,32})", html or ""):
        yield m.group(1)
