"""Phase 2b -- fetch everything about every thread.

The only phase that touches the network. Everything it retrieves lands in
RawStore, so later phases (and later reruns with better logic) need no API.

Per thread:
  V1 bulk      metadata the V2 API does not expose, plus V1 HTML for live-app
               names only -- it is never rendered
  V2 bulk      secret_path, spreadsheet_summary, sharing
  V2 html      the authoritative content, cursor-paginated
  messages     comments and chat, paged backwards
  V2 sharing   members / folders / invited-members  (--sharing full)
  blobs        every image and attachment referenced by the content
"""

from __future__ import annotations

import json
import logging
import re
import time

from . import quipclient as q
from .model import Thread
from .state import JsonlFile

log = logging.getLogger(__name__)

THREAD_BATCH = 25          # bulk metadata calls
USER_BATCH = 500           # name resolution
MESSAGE_PAGE = 100

# One bad document is ordinary; this many in a row means the run is broken
# rather than the content, and continuing would mark the whole account bad.
MAX_CONSECUTIVE_FAILURES = 25


class Incomplete(RuntimeError):
    """Some part of this thread never answered, so it is not finished.

    Raised rather than recorded, because the ledger marks a thread done only
    when nothing raised -- and a thread marked done is skipped by every later
    run. Anything the server *did* answer (403 on a blob, 400 on a workbook) is
    not this: those are final, and are reported per document instead.
    """

# V1 emits src='/blob/T/B'; V2 emits src='https://quip.com/blob/T/B'. Both, plus
# href= forms for file attachments.
BLOB_RE = re.compile(
    r"""(?:src|href)\s*=\s*['"](?:https?://[^/'"]*quip\.com)?/blob/([\w-]+)/([\w-]+)[^'"]*['"]""",
    re.IGNORECASE,
)

# Any quip.com link inside a document body: another document, a folder, or an
# @mention of a person. Which one it is can only be settled by asking.
MENTION_RE = re.compile(r"https?://[\w.-]*quip\.com/([\w-]{8,32})")


class _Heartbeat:
    """Says something at least every `every` seconds.

    A phase that is working normally but silent is indistinguishable from one
    that has hung, and on a rate-limited account both look like nothing
    happening for minutes at a time. Count-based progress is not enough on its
    own: ten threads is a minute on a fast account and ten minutes on a slow
    one.
    """

    def __init__(self, every: float = 60.0):
        self.every = every
        self.last = time.time()

    def due(self) -> bool:
        now = time.time()
        if now - self.last >= self.every:
            self.last = now
            return True
        return False


def harvest(client: q.QuipClient, cfg, ledger, progress=None, backfill=False) -> dict:
    """Fetch every discovered thread. Safe to interrupt and resume.

    `backfill=True` ignores the ledger and walks every thread again. That is
    what you want when the exporter learns to fetch something new -- the XLSX
    export, say -- for an account already harvested: RawStore serves everything
    previously fetched from disk, so only the genuinely new calls hit the
    network.
    """
    threads_file = JsonlFile(cfg.state_dir / "threads.jsonl")
    if not threads_file.exists():
        raise RuntimeError("Run discover first: state/threads.jsonl is missing")

    entries = list(threads_file.read_all())
    thread_ids = [e["id"] for e in entries]
    total = len(thread_ids)
    log.info("Harvesting %d threads (sharing=%s)", total, cfg.sharing)

    if backfill:
        pending = list(thread_ids)
        log.info("  backfill: revisiting all %d threads; cached calls cost nothing", total)
    else:
        pending = [t for t in thread_ids if not ledger.is_done("harvest", t)]
        if len(pending) < total:
            log.info("  %d already harvested; %d remaining",
                     total - len(pending), len(pending))

    stats = {"threads": 0, "blobs": 0, "blob_errors": 0, "messages": 0, "errors": 0,
             "xlsx": 0, "xlsx_errors": 0}
    consecutive_failures = 0
    beat = _Heartbeat(60.0)
    user_ids: set = set()
    link_tokens: set = set()      # ids/secret paths mentioned inside document bodies

    # Bulk metadata first: one call per 25 threads, and it tells us which
    # threads are chats before we try to fetch document HTML for them.
    meta_v1, meta_v2 = _fetch_metadata(client, thread_ids, stats, pending=pending)

    for i, tid in enumerate(pending, 1):
        try:
            rec1, rec2 = meta_v1.get(tid), meta_v2.get(tid)
            # An unreadable thread comes back *present* with an `error` key, so
            # absence means the batch call itself failed. Proceeding would fetch
            # the thread without its V2 record -- no `spreadsheet_summary`, so no
            # workbook -- and still mark it done, stranding the gap.
            if rec1 is None or rec2 is None:
                missing = " and ".join(
                    n for n, r in (("V1", rec1), ("V2", rec2)) if r is None)
                raise Incomplete(f"{missing} bulk metadata did not answer")

            thread = Thread.from_raw(tid, rec1, rec2)
            if not thread.title and not thread.link:
                log.warning("  %s unavailable (no metadata); recording and skipping", tid)
                ledger.mark("harvest", tid, status="unavailable")
                stats["errors"] += 1
                continue

            # Deleted content is out of scope.
            if thread.is_deleted:
                ledger.mark("harvest", tid, status="deleted")
                continue

            _fetch_content(client, thread, cfg, stats)
            _fetch_messages(client, thread, stats)
            if cfg.sharing == "full":
                _fetch_sharing(client, thread)
            _fetch_blobs(client, thread, cfg, stats)
            _fetch_xlsx(client, thread, stats)

            user_ids.update(thread.expanded_user_ids)
            user_ids.update(thread.member_ids)
            if thread.author_id:
                user_ids.add(thread.author_id)
            for m in thread.messages:
                if m.get("author_id"):
                    user_ids.add(m["author_id"])
                user_ids.update(m.get("mention_user_ids") or [])
                link_tokens.update(MENTION_RE.findall(m.get("text") or ""))
            # @mentions of people inside the body reference users who may not be
            # members of anything we have seen; without resolving them the link
            # pass cannot turn the mention into a plain name.
            link_tokens.update(MENTION_RE.findall(thread.html or ""))

            # Record which sharing mode fetched this thread. `audit` demands the
            # membership chain only under `--sharing full`, so a run that
            # switched modes mid-way would otherwise be judged by whichever flag
            # happened to be on the audit command -- and an `until audit; do
            # harvest; done` loop whose two halves disagree spins forever
            # without fetching anything.
            ledger.mark("harvest", tid, status="done", sharing=cfg.sharing,
                        title=thread.display_title[:80])
            stats["threads"] += 1
            consecutive_failures = 0
        except q.QuipAuthError:
            # The token, not the thread. Continuing would mark the rest of the
            # account as errors and exit zero.
            log.error("Token rejected (HTTP 401) -- stopping. Nothing after this "
                      "point was fetched; re-run with a valid token to continue.")
            raise
        except Exception as e:                      # never let one thread stop the run
            log.exception("  %s failed: %s", tid, e)
            ledger.mark("harvest", tid, status="error", error=str(e)[:200])
            stats["errors"] += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # One bad document is normal. Many in a row is the run being
                # broken -- revoked token, network gone, disk full -- and
                # grinding on would fill the ledger with errors nobody reads.
                raise RuntimeError(
                    f"{consecutive_failures} threads failed in a row; stopping "
                    f"before the rest of the account is marked bad. Last error: {e}"
                ) from e

        if progress and (i % 10 == 0 or i == len(pending) or beat.due()):
            progress(i, len(pending), stats)

    # Scan every thread's cached HTML, not just the ones fetched this run, so a
    # resumed harvest still resolves mentions. Reading the cache is free.
    link_tokens.update(_collect_link_tokens(client.store, thread_ids))

    # Tokens in link position that are not threads or secret paths we know about
    # are candidate user ids. Asking settles it; the answer is cached either way.
    known = set(thread_ids)
    for rec in list(meta_v2.values()):
        sp = (rec or {}).get("thread", {}).get("secret_path") if isinstance(rec, dict) else None
        if sp:
            known.add(sp)
    user_ids.update(link_tokens - known)

    _fetch_users(client, cfg, user_ids)
    log.info("Harvest complete: %s", stats)
    return stats


# -- pieces --------------------------------------------------------------

def _fetch_metadata(client, thread_ids, stats, pending=None) -> tuple[dict, dict]:
    """Two bulk calls per 25 threads, before any per-thread work starts.

    Batches are cut from the **full discovered list**, not from what is still
    pending, and only batches containing a pending thread are requested. The
    id-tuple of a batch is therefore identical on every run, so a resumed
    harvest reads its metadata from the cache instead of the network.

    Cutting them from the shrinking pending list realigned every boundary after
    each resume, making every batch a new cache key: measured on a real account,
    285 calls for 143 distinct batches, with some threads described six times.
    That is a fixed ~2.4 minutes of rate-limited re-fetching on every restart,
    paid again on each pass of an `until audit; do harvest; done` loop.

    On a large account this phase is minutes of work on its own, so it reports
    as it goes rather than going quiet right after "Harvesting N threads".
    """
    v1, v2 = {}, {}
    pending = set(pending) if pending is not None else None
    windows = [thread_ids[i : i + THREAD_BATCH]
               for i in range(0, len(thread_ids), THREAD_BATCH)]
    wanted = [w for w in windows
              if pending is None or any(t in pending for t in w)]
    batches = len(wanted)
    if batches > 1:
        skipped = len(windows) - batches
        log.info("  fetching bulk metadata: %d batches of %d%s", batches,
                 THREAD_BATCH,
                 f" ({skipped} already complete, skipped)" if skipped else "")
    beat = _Heartbeat(30.0)
    for i, batch in enumerate(wanted):
        got1 = q.threads_v1(client, batch)
        got2 = q.threads_v2(client, batch)
        if got1:
            v1.update(got1)
        if got2:
            v2.update(got2)
        if not got1 and not got2:
            stats["errors"] += 1
            log.warning("  metadata batch failed for %d threads", len(batch))
        done = i + 1
        if beat.due() or done == batches:
            log.info("  bulk metadata: batch %d/%d (%d threads described)",
                     done, batches, len(v2))
    return v1, v2


def _fetch_content(client, thread: Thread, cfg, stats) -> None:
    """V2 HTML only. V1 HTML is kept for live-app names but never rendered.

    Raises on a partially-paginated document. That is deliberate: the caller
    records a thread as done only when nothing raised, so a truncated fetch
    leaves the thread pending and the next run finishes it. Returning the
    partial text instead is how an interrupted run ships 40% of a document and
    calls itself complete.
    """
    if thread.is_chat:
        return                                   # chats have no document body
    html, pages, complete = q.thread_html_v2(client, thread.id)
    thread.html = html
    thread.html_pages = pages
    thread.html_complete = complete
    if not complete:
        raise RuntimeError(
            f"HTML incomplete after {pages} page(s): the cursor chain did not "
            "terminate, so this thread stays pending for the next run")
    if not html:
        thread.errors.append("no HTML returned")
        log.warning("  %s (%s): no HTML", thread.id, thread.display_title[:40])
    elif pages > 1:
        log.info("  %s: HTML reassembled from %d pages", thread.id, pages)


def _fetch_messages(client, thread: Thread, stats) -> None:
    """Page backwards through messages, as the baqup sample does.

    Raises `Incomplete` if a page never answers. The old code broke out of the
    loop on failure, which is indistinguishable from reaching the end -- so a
    blip mid-pagination truncated the comment history and the thread was still
    recorded as fully harvested.
    """
    collected, max_created = [], None
    seen = set()
    while True:
        params = {"count": MESSAGE_PAGE}
        if max_created is not None:
            params["max_created_usec"] = max_created
        resp = client.get(f"/1/messages/{thread.id}", **params)
        if not resp.ok:
            if q.is_answer(resp.status):
                # 403 on a thread we may not read: a real answer, and the
                # export already reports it. Nothing more to fetch.
                thread.errors.append(f"messages unavailable (HTTP {resp.status})")
                break
            raise Incomplete(
                f"messages page {len(collected) // MESSAGE_PAGE + 1} did not "
                f"answer (HTTP {resp.status})")
        try:
            chunk = resp.json()
        except (ValueError, UnicodeDecodeError):
            raise Incomplete("messages page is not readable JSON")
        if not isinstance(chunk, list) or not chunk:
            break
        fresh = [m for m in chunk if m.get("id") not in seen]
        if not fresh:
            break
        seen.update(m.get("id") for m in fresh)
        collected.extend(fresh)
        stamps = [m.get("created_usec") for m in chunk if m.get("created_usec")]
        if not stamps or len(chunk) < MESSAGE_PAGE:
            break
        nxt = min(stamps) - 1
        if max_created is not None and nxt >= max_created:
            break
        max_created = nxt

    # Deleted messages are out of scope.
    collected = [m for m in collected if m.get("visible", True) is not False]
    collected.sort(key=lambda m: m.get("created_usec") or 0)
    thread.messages = collected
    stats["messages"] += len(collected)


def _fetch_sharing(client, thread: Thread) -> None:
    """Membership, containing folders and pending invitations.

    Raises on a partially-paginated result for the same reason as the content:
    an incomplete member list would be recorded as the whole list, and folder
    membership decides where the document is filed.
    """
    incomplete = []

    pages, complete = client.paginate_all(f"/2/threads/{thread.id}/members")
    if not complete:
        incomplete.append("members")
    for page in pages:
        for m in page.get("members") or []:
            if m.get("user_id"):
                thread.member_ids.append(m["user_id"])
                thread.access_levels.setdefault(m["user_id"],
                                                {"access_level": m.get("access_level")})

    pages, complete = client.paginate_all(f"/2/threads/{thread.id}/folders")
    if not complete:
        incomplete.append("folders")
    for page in pages:
        for entry in page.get("folders") or []:
            entries = entry if isinstance(entry, list) else [entry]
            for f in entries:
                if isinstance(f, dict) and f.get("folder_id") not in thread.folder_ids:
                    thread.folder_ids.append(f["folder_id"])

    pages, complete = client.paginate_all(f"/2/threads/{thread.id}/invited-members")
    if not complete:
        incomplete.append("invited-members")
    for page in pages:
        for m in page.get("invited_members") or []:
            if m.get("email") and m["email"] not in thread.invited_emails:
                thread.invited_emails.append(m["email"])

    if incomplete:
        raise RuntimeError(f"sharing incomplete ({', '.join(incomplete)}); "
                           "thread stays pending for the next run")


def _fetch_blobs(client, thread: Thread, cfg, stats) -> None:
    """Download every referenced blob.

    A 403 here is expected and is not a document failure: blob permissions are
    independent of thread permissions, so a readable document can have
    unreadable images.
    """
    refs = set()
    for html in (thread.html, thread.v1_html):
        for tid, bid in BLOB_RE.findall(html or ""):
            refs.add((tid, bid))
    for m in thread.messages:
        for f in m.get("files") or []:
            if f.get("hash"):
                refs.add((thread.id, f["hash"]))

    for tid, bid in sorted(refs):
        resp = q.blob(client, tid, bid)
        if resp.ok and not resp.body.lstrip().startswith(b'{"error'):
            stats["blobs"] += 1
        elif q.is_answer(resp.status):
            # 403 is expected and documented: blob permissions are independent
            # of thread permissions. A real answer, flagged per document.
            stats["blob_errors"] += 1
            thread.errors.append(f"blob {bid} unavailable (HTTP {resp.status})")
        else:
            # Never answered. Leaving the thread "done" would strand the image
            # forever, since a resumed harvest skips completed threads.
            raise Incomplete(f"blob {bid} did not answer (HTTP {resp.status})")


def _fetch_xlsx(client, thread: Thread, stats) -> None:
    """Download the real workbook for any thread carrying spreadsheet data.

    The HTML flattens a spreadsheet to a plain `<table>`: cell types, number
    formats, column widths and the separation into sheets are all lost, and an
    ordinary document with three embedded sheets becomes three anonymous
    tables. The XLSX keeps all of that.

    Gated on `spreadsheet_summary` because that is the reliable detector for a
    spreadsheet, embedded or standalone -- Quip rejects anything else with 400,
    and there is no reason to spend a call learning that.
    """
    if thread.is_chat or not thread.spreadsheet_summary:
        return
    resp = q.export_xlsx(client, thread.id)
    if resp.ok and resp.body[:2] == b"PK":
        stats["xlsx"] += 1
    elif q.is_answer(resp.status):
        # 400 means Quip declined to build a workbook for this thread. Final.
        stats["xlsx_errors"] += 1
        thread.errors.append(f"xlsx export unavailable (HTTP {resp.status})")
        log.warning("  %s (%s): no xlsx (HTTP %s)", thread.id,
                    thread.display_title[:40], resp.status)
    else:
        raise Incomplete(f"xlsx export did not answer (HTTP {resp.status})")


def _collect_link_tokens(store, thread_ids) -> set:
    """Every quip.com token appearing in any cached document body. Offline."""
    tokens = set()
    if store is None:
        return tokens
    for tid in thread_ids:
        html = store.load_thread_html(tid)
        if html:
            tokens.update(MENTION_RE.findall(html))
    return tokens


def _fetch_users(client, cfg, user_ids: set) -> None:
    """Resolve names in batches.

    Some ids are guesses harvested from link positions, so a batch can be
    rejected outright. Fall back to smaller chunks rather than losing the whole
    batch to one bad id.
    """
    ids = sorted(u for u in user_ids if u)
    if not ids:
        return
    log.info("Resolving %d user names", len(ids))
    resolved = 0
    for i in range(0, len(ids), USER_BATCH):
        batch = ids[i : i + USER_BATCH]
        got = q.users(client, batch)
        if got is None and len(batch) > 1:
            for j in range(0, len(batch), 25):
                sub = q.users(client, batch[j : j + 25])
                resolved += sum(1 for v in (sub or {}).values() if isinstance(v, dict))
        else:
            resolved += sum(1 for v in (got or {}).values() if isinstance(v, dict))
    log.info("  resolved %d of %d", resolved, len(ids))
