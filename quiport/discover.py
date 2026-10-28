"""Phase 2a -- enumerate every folder and thread reachable from the account.

Writes `state/folders.jsonl` and `state/threads.jsonl` once, at the end of the
walk -- an interrupted `discover` leaves the previous lists untouched rather
than a half-written one. Every API response is cached as it arrives, so a
re-run after an interruption replays from disk and costs almost nothing.
`threads.jsonl` is *merged*, never replaced: threads added by `refresh` or
`check-updates` survive a later `discover`.

Trash is deliberately excluded: deleted content is out of scope.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import quipclient as q
from .model import Folder
from .state import JsonlFile

log = logging.getLogger(__name__)

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


FOLDER_BATCH = 50
RECENT_PAGE = 50


def root_folder_ids(user: dict, include_archive=True, include_starred=True) -> list[tuple[str, str]]:
    """(folder_id, role) for each root. Trash is never included."""
    roots = [(user.get("private_folder_id"), "private"),
             (user.get("desktop_folder_id"), "desktop")]
    if include_archive:
        roots.append((user.get("archive_folder_id"), "archive"))
    if include_starred:
        roots.append((user.get("starred_folder_id"), "starred"))
    roots += [(fid, "shared") for fid in (user.get("shared_folder_ids") or [])]
    roots += [(fid, "group") for fid in (user.get("group_folder_ids") or [])]

    seen, out = set(), []
    for fid, role in roots:
        if fid and fid not in seen:
            seen.add(fid)
            out.append((fid, role))
    return out


def discover(client: q.QuipClient, cfg, ledger=None) -> dict:
    """Walk the folder graph, then sweep for threads it could not reach."""
    folders_file = JsonlFile(cfg.state_dir / "folders.jsonl")
    threads_file = JsonlFile(cfg.state_dir / "threads.jsonl")

    user = q.current_user(client)
    if not user:
        raise RuntimeError("Cannot read /1/users/current -- is the token valid?")
    log.info("Authenticated as %s (%s)", user.get("name"), user.get("id"))
    (cfg.state_dir / "user.json").write_text(
        __import__("json").dumps(user, indent=1), encoding="utf-8")

    roots = root_folder_ids(user, cfg.include_archive, cfg.include_starred)
    log.info("Walking %d root folders", len(roots))

    folders: dict[str, Folder] = {}
    parent_of: dict[str, str] = {}
    thread_folders: dict[str, list[str]] = {}   # thread_id -> [folder_id...]
    restricted: list[str] = []
    order: list[str] = []                        # deterministic folder order

    queue = [fid for fid, _ in roots]
    while queue:
        batch, queue = queue[:FOLDER_BATCH], queue[FOLDER_BATCH:]
        batch = [f for f in batch if f not in folders]
        if not batch:
            continue
        data, unanswered = _fetch_folder_batch(client, batch)
        if unanswered:
            # Never silently absorb this. Treating a failed batch call as 50
            # restricted folders would drop every subtree beneath them and
            # report it as a permissions problem -- an account that quietly
            # discovers half of itself.
            raise RuntimeError(
                f"{len(unanswered)} folder(s) could not be read and the server "
                f"gave no answer about them: {', '.join(unanswered[:5])}"
                f"{'...' if len(unanswered) > 5 else ''}. Discovery stopped "
                "rather than under-report the account; re-run `discover` -- "
                "everything already fetched is cached, so it resumes cheaply.")
        for fid in batch:                       # iterate our order, not the API's
            record = data.get(fid)
            if not isinstance(record, dict) or "folder" not in record:
                log.warning("Folder %s unavailable: %s", fid, str(record)[:120])
                restricted.append(fid)
                continue
            folder = Folder.from_raw(fid, record)
            folder.parent_id = parent_of.get(fid, "")
            folders[fid] = folder
            order.append(fid)

            for tid in folder.child_thread_ids:
                thread_folders.setdefault(tid, [])
                if fid not in thread_folders[tid]:
                    thread_folders[tid].append(fid)

            for child in folder.child_folder_ids:
                if child not in folders and child not in parent_of:
                    parent_of[child] = fid
                    queue.append(child)

            restricted.extend(folder.restricted_child_ids)

        log.info("  %d folders, %d threads so far", len(folders), len(thread_folders))

    # Threads the folder walk cannot see: owned but unfiled, and conversations.
    extra = _sweep_unfiled(client, set(thread_folders),
                           deep=not getattr(cfg, 'quick_sweep', False))
    for tid in extra:
        thread_folders.setdefault(tid, [])

    folders_file.write_all([
        {"id": f.id, "title": f.title, "folder_type": f.folder_type,
         "color": f.color,
         "created_usec": f.created_usec, "updated_usec": f.updated_usec,
         "link": f.link, "creator_id": f.creator_id, "member_ids": f.member_ids,
         "parent_id": f.parent_id, "child_thread_ids": f.child_thread_ids,
         "child_folder_ids": f.child_folder_ids,
         "restricted_child_ids": f.restricted_child_ids}
        for f in (folders[fid] for fid in order)
    ])
    # Discovery is **additive**. A thread this export already knows about stays
    # known even if this walk did not reach it.
    #
    # Not hypothetical: `refresh` and `check-updates` add threads that the folder
    # walk cannot see -- they come from
    # `/1/users/current/threads-modified-after-usec`, which reports threads no
    # folder lists and the recent sweep misses. Replacing the file wholesale
    # erased two of them, and the next render deleted their pages as stale.
    #
    # A thread genuinely deleted in Quip is handled elsewhere: harvest sees
    # `is_deleted` or a 403 and records that. Dropping it from the list here
    # would instead make it silently cease to exist.
    previous = list(threads_file.read_all())
    merged: dict[str, list] = {r["id"]: list(r.get("folder_ids") or [])
                               for r in previous}
    for tid, fids in thread_folders.items():
        slot = merged.setdefault(tid, [])
        for fid in fids:
            if fid not in slot:
                slot.append(fid)
    carried = [t for t in merged if t not in thread_folders]
    if carried:
        log.info("  carrying forward %d thread(s) this walk did not reach "
                 "(added by refresh, or no longer filed): %s",
                 len(carried), ", ".join(carried[:5]))
    threads_file.write_all([{"id": t, "folder_ids": f} for t, f in merged.items()])

    # Reaching here means every folder batch answered and both sweeps ran to
    # their end -- each failure path above raises. Recorded so "was discovery
    # complete?" is answerable from disk later, rather than inferred from the
    # fact that a number looks plausible.
    summary = {
        "folders": len(folders),
        # The export's total, not this walk's -- they differ whenever a thread
        # was added by `refresh` and no folder lists it.
        "threads": len(merged),
        "threads_reached_by_this_walk": len(thread_folders),
        "carried_forward": len(carried),
        "restricted_folders": sorted(set(restricted)),
        "unfiled_threads": len(extra),
        "user_id": user.get("id"), "user_name": user.get("name"),
        "site": user.get("url"),
        "discovery_complete": True,
        "completed_at": _now(),
    }
    log.info("Discovered %d folders, %d threads (%d unfiled, %d carried forward, "
             "%d restricted folders)", summary["folders"], summary["threads"],
             summary["unfiled_threads"], summary["carried_forward"],
             len(summary["restricted_folders"]))
    return summary


def _fetch_folder_batch(client, batch: list) -> tuple[dict, list]:
    """Fetch a batch of folders, telling a refusal apart from a failed call.

    `/1/folders/?ids=` answers for 50 folders at once, so one transient failure
    costs 50 folders and everything below them. When the batch does not answer,
    each id is asked for individually: that isolates a genuinely restricted
    folder (the server answers, with an error record or a 4xx) from one we
    simply failed to reach (5xx, dropped connection), which must be retried
    rather than recorded as inaccessible.

    Rate limiting cannot land here -- 429 and 503 are retried indefinitely by
    the client -- so anything that reaches this path is a real failure.

    Returns (data, ids_that_never_answered).
    """
    resp = client.get("/1/folders/", ids=",".join(batch), include_chats="true")
    if resp.ok:
        try:
            return resp.json(), []
        except (ValueError, UnicodeDecodeError):
            log.warning("folder batch returned unreadable JSON; retrying singly")

    data, unanswered = {}, []
    for fid in batch:
        one = client.get("/1/folders/", ids=fid, include_chats="true")
        if one.ok:
            try:
                data.update(one.json())
                continue
            except (ValueError, UnicodeDecodeError):
                pass
        if q.is_answer(one.status):
            # A real "no": recorded as restricted by the caller.
            data[fid] = {"error": f"HTTP {one.status}"}
        else:
            unanswered.append(fid)
    return data, unanswered


def _sweep_unfiled(client: q.QuipClient, known: set, deep: bool = True) -> set:
    """Catch threads no folder pointed at, asking three different questions.

    Finding every thread matters more than finishing quickly, so all three run
    by default and the results are unioned. `--quick-sweep` drops the third.

      * `/1/users/current/threads` -- despite the name, **not** limited to
        threads the user authored. Measured on a real account it returned 1592
        threads, 283 of which had a different `author_id`; it is the user's
        accessible-thread list.
      * `/1/users/current/threads-modified-after-usec` from 0 -- every thread
        the user can see, which is how a thread in nobody's folder turns up
      * `/1/threads/recent` -- an independent query, and the one that costs.
        It cannot be asked to omit content: `threads_meta=true` is ignored and
        every page carries the full HTML of 50 documents. On that same account
        it returned 1036 threads in 30.7 MB, against 211 KB for the first call.
        It found nothing the others missed *there*, which is a result about one
        account, not a proof -- so it stays on, and instead announces itself so
        the wait does not read as a hang.

    The first two accept `threads_meta=true` and return ids rather than content.
    """
    found: set = set()

    def paged(endpoint, label, **params):
        added, cursor, pages = 0, None, 0
        while True:
            resp = client.get(endpoint, cursor=cursor, **params)
            # "No more pages" and "the call failed" are different answers, and
            # `break` on either silently truncates the sweep.
            if not resp.ok:
                raise RuntimeError(
                    f"sweep of {label} failed (HTTP {resp.status}); stopping "
                    "rather than under-report. Re-run `discover` to resume.")
            try:
                page = resp.json()
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError(f"sweep of {label} returned unreadable JSON")
            if not page:
                break
            pages += 1
            ids = list((page.get("threads") or {}).keys())
            new = [i for i in ids if i not in known and i not in found]
            found.update(new)
            added += len(new)
            # Without this the sweep looks hung -- dozens of calls with nothing
            # to show, after a folder walk that printed a line per batch.
            log.info("  sweeping %s: page %d, %d seen, %d new", label, pages,
                     len(ids), added)
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not ids:
                break
        return added

    paged("/1/users/current/threads", "accessible threads",
          limit=100, threads_meta="true")
    paged("/1/users/current/threads-modified-after-usec", "all visible threads",
          last_modified_after_usec=0, limit=100, threads_meta="true")

    if deep:
        # Minutes of silence on a large account otherwise -- say so first.
        log.info("  verifying with a third enumeration (/1/threads/recent); "
                 "it returns full document content, so this can take several "
                 "minutes on a large account. It is not stuck.")
        extra = _sweep_recent(client, known | found)
        if extra:
            log.info("  verification found %d thread(s) the cheaper "
                     "enumerations missed", len(extra))
        else:
            log.info("  verification agreed: no additional threads")
        found |= extra

    if found:
        log.info("  %d thread(s) found outside the folder tree", len(found))
    return found


def _sweep_recent(client: q.QuipClient, known: set) -> set:
    """The `/1/threads/recent` walk -- a third, independent enumeration.

    Kept on by default because it is a genuinely different query and this
    project does not guess about completeness. It is the expensive one: every
    page carries the full HTML of 50 documents to yield 50 ids.
    """
    found, seen_ids, max_updated, pages, sent = set(), set(), None, 0, 0
    while True:
        resp = client.get("/1/threads/recent",
                          count=RECENT_PAGE, max_updated_usec=max_updated)
        if not resp.ok:
            raise RuntimeError(
                f"verification sweep failed (HTTP {resp.status}); stopping rather than "
                "under-report. Re-run `discover` to resume.")
        try:
            page = resp.json()
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("verification sweep returned unreadable JSON")
        if not page:
            break
        pages += 1
        sent += len(resp.body)
        records = list(page.values())
        new_ids = {r.get("thread", {}).get("id") for r in records if isinstance(r, dict)}
        new_ids.discard(None)
        if not new_ids or new_ids <= seen_ids:
            break
        seen_ids |= new_ids
        found.update(i for i in new_ids if i not in known)
        log.info("  verifying: page %d, %d seen, %d new (%.1f MB downloaded)",
                 pages, len(new_ids), len(found), sent / 1e6)
        stamps = [r["thread"].get("updated_usec") for r in records
                  if isinstance(r, dict) and r.get("thread", {}).get("updated_usec")]
        if not stamps:
            break
        nxt = min(stamps) - 1
        if max_updated is not None and nxt >= max_updated:
            break
        max_updated = nxt
    return found
