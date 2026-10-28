"""Is the raw cache complete?

`verify` checks the rendered export. This checks the thing it was built from,
which is the only question that matters after an interrupted harvest: **did we
actually get everything, or does the cache merely look plausible?**

The distinction is not academic. A cursor chain that stops halfway leaves a
document that renders perfectly and contains 40% of its content, and `verify`
cannot see it -- its text comparison reads from this same cache, so both sides
of the comparison are equally wrong. Only walking the chain and demanding that
it *terminated* catches that.

Reads from the content-addressed files, never from `index.jsonl`, so a lost
index line cannot hide a gap.

    python3 -m quiport audit --export-id <id>
    python3 -m quiport audit --export-id <id> --repair --token ...

Exit code is non-zero if anything is incomplete, so it works in a script and can
gate rendering.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .model import Thread, unwrap
from .quipclient import is_answer
from .state import JsonlFile

log = logging.getLogger(__name__)

BLOB_RE = re.compile(
    r"""(?:src|href)\s*=\s*['"](?:https?://[^/'"]*quip\.com)?/blob/([\w-]+)/([\w-]+)[^'"]*['"]""",
    re.IGNORECASE)


@dataclass
class ThreadAudit:
    """What is missing for one thread, split by whether asking again can help.

    `gaps` are retryable: never fetched, or a chain that stops because a page is
    absent. `refused` is the server's final word -- a 403 on a thread this token
    cannot read. Conflating the two would mean the audit can never report
    complete, which would destroy its value as a gate on rendering.
    """

    thread_id: str
    title: str = ""
    is_chat: bool = False
    gaps: list = field(default_factory=list)       # (kind, detail) -- retryable
    refused: list = field(default_factory=list)    # (kind, detail) -- permanent
    calls_needed: list = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.gaps

    def gap(self, kind: str, detail: str = "", refetch=None, status: int = 0) -> None:
        """Record a shortfall, classified by whether a retry could fix it."""
        if is_answer(status) and not (200 <= status < 300):
            self.refused.append((kind, detail))
            return
        self.gaps.append((kind, detail))
        if refetch is not None:
            self.calls_needed.append(refetch)


def _bulk_metadata(raw) -> tuple[dict, dict]:
    """Merge every cached bulk-metadata response, reading files directly.

    Deliberately not via `index.jsonl`: the index is a convenience, and the
    audit's job includes catching the case where the index has lost an entry
    that the files still hold.
    """
    v1, v2 = {}, {}
    for meta_path in raw.root.rglob("*.meta.json"):
        if "superseded" in meta_path.parts or "failed" in meta_path.parts:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        canon = meta.get("canonical", "")
        if canon.startswith("GET /1/threads/?ids="):
            target = v1
        elif canon.startswith("GET /2/threads/?ids="):
            target = v2
        else:
            continue
        if not (200 <= meta.get("status", 0) < 300):
            continue
        body = meta_path.with_name(meta.get("body_file") or "")
        if not body.exists():
            continue
        try:
            data = json.loads(body.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            target.update(data)
    return v1, v2


def audit_thread(raw, tid: str, rec1, rec2, sharing: str) -> ThreadAudit:
    """Everything one thread needs, checked against the cache."""
    result = ThreadAudit(thread_id=tid)

    if not rec1 and not rec2:
        result.gap("metadata", "no bulk metadata cached")
        return result                        # nothing else is knowable
    if isinstance(rec1, dict) and "error" in rec1 and "thread" not in rec1:
        # A real answer: the thread exists but is not readable by this token.
        result.title = tid
        return result

    thread = Thread.from_raw(tid, rec1, rec2)
    result.title = thread.display_title
    result.is_chat = thread.is_chat
    if thread.is_deleted:
        return result                        # out of scope by design

    if not rec1:
        result.gap("metadata", "V1 bulk metadata missing")
    if not rec2:
        result.gap("metadata", "V2 bulk metadata missing")

    # -- content: the chain must have ended, not merely started --------
    html = ""
    if not thread.is_chat:
        chain = raw.html_chain(tid)
        html = chain["html"]
        if not chain["complete"]:
            result.gap("html", chain["reason"], status=chain.get("status", 0),
                       refetch=(f"/2/threads/{tid}/html", {}))

    # -- messages -----------------------------------------------------
    messages = raw.message_chain(tid)
    if not messages["complete"]:
        result.gap("messages", messages["reason"], status=messages.get("status", 0),
                   refetch=(f"/1/messages/{tid}", {"count": 100}))

    # -- sharing ------------------------------------------------------
    if sharing == "full" and not thread.is_chat:
        for name in ("members", "folders", "invited-members"):
            # The whole chain, not just page one: membership readily runs to
            # several pages, and harvest follows them.
            chain = raw.cursor_chain(f"/2/threads/{tid}/{name}")
            if not chain["complete"]:
                result.gap("sharing", f"{name}: {chain['reason']}",
                           status=chain.get("status", 0),
                           refetch=(f"/2/threads/{tid}/{name}", {}))

    # -- blobs referenced by the content we have ----------------------
    for blob_thread, blob_id in sorted(set(BLOB_RE.findall(html or ""))):
        resp = raw.load("GET", f"/1/blob/{blob_thread}/{blob_id}", {})
        if resp is None:
            result.gap("blob", f"{blob_id} not fetched",
                       refetch=(f"/1/blob/{blob_thread}/{blob_id}", {}))
        # A cached 403/404 is a real answer: the image is unreadable, which the
        # export already reports per document. Not a gap in the harvest.

    # -- xlsx for anything carrying spreadsheet data ------------------
    if thread.spreadsheet_summary and not thread.is_chat:
        resp = raw.load("GET", f"/1/threads/{tid}/export/xlsx", {})
        if resp is None:
            result.gap("xlsx", "workbook not fetched",
                       refetch=(f"/1/threads/{tid}/export/xlsx", {}))
        elif resp.ok and not resp.body.startswith(b"PK"):
            result.gap("xlsx", "cached response is not a workbook",
                       refetch=(f"/1/threads/{tid}/export/xlsx", {}))

    return result


def audit_discovery(cfg) -> list:
    """Did discovery finish, or merely stop?

    Every failure path in `discover` raises, so the flag is only written on a
    clean walk. An export whose discovery never completed is under-counting the
    account, and no amount of auditing the threads it *did* find would reveal
    that -- the missing ones were never written down.
    """
    path = cfg.state_dir / "discovery.json"
    if not path.exists():
        return [("discovery", "discovery.json is missing; run discover")]
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [("discovery", "discovery.json is unreadable; re-run discover")]
    if not summary.get("discovery_complete"):
        return [("discovery",
                 "discovery did not record completion -- it was either "
                 "interrupted or predates this check. Re-run `discover`; "
                 "cached calls make it cheap.")]
    return []


def audit_forgotten_threads(cfg) -> list:
    """Threads the ledger says were harvested but the thread list no longer has.

    The audit otherwise checks that everything *in* `threads.jsonl` is complete,
    which cannot see a thread falling out of that list -- and a thread not in the
    list is not rendered, and its page is then deleted as stale. Silent, and
    invisible to every other check.

    That is exactly what a wholesale re-write of the list by `discover` used to
    do to threads added by `refresh`.
    """
    from .state import Ledger

    listed = {r["id"] for r in JsonlFile(cfg.state_dir / "threads.jsonl").read_all()}
    ledger_path = cfg.state_dir / "ledger.jsonl"
    if not ledger_path.exists():
        return []
    harvested = set()
    for rec in JsonlFile(ledger_path).read_all():
        if rec.get("phase") == "harvest" and rec.get("status") == "done":
            harvested.add(str(rec.get("unit")))
    missing = sorted(harvested - listed)
    return [("forgotten", f"{tid} was harvested but is no longer in "
                          "threads.jsonl, so it is absent from the export")
            for tid in missing]


def audit_folders(raw, cfg) -> list:
    """Folders discovery reached, and children it recorded but never fetched."""
    gaps = []
    records = list(JsonlFile(cfg.state_dir / "folders.jsonl").read_all())
    known = {r["id"] for r in records}
    for rec in records:
        for child in rec.get("child_folder_ids") or []:
            if child not in known:
                gaps.append(("folder", f"{child} is a child of {rec['id']} "
                                       "but was never fetched"))
    if not records:
        gaps.append(("folder", "no folders discovered; run discover"))
    return gaps


def non_answers(raw) -> list:
    """Cached responses that were never really answered.

    Status 0, 5xx and 429 mean the request failed rather than the resource
    replying. `RawStore.load` refuses to serve them, so each one is a call still
    outstanding -- and on a long run they are the residue of network trouble.
    """
    out = []
    for meta_path in raw.root.rglob("*.meta.json"):
        if "superseded" in meta_path.parts or "failed" in meta_path.parts:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append(("unreadable", str(meta_path.name)))
            continue
        status = meta.get("status", 0)
        if not is_answer(status):
            out.append((meta.get("canonical", meta_path.name), status))
    return out


def index_drift(raw) -> dict:
    """Does `index.jsonl` still describe what is on disk?

    The renderer reads bulk metadata and users through the index, so a line lost
    to an interrupted append silently removes threads from the export. Compared
    in both directions.
    """
    on_disk = set()
    for meta_path in raw.root.rglob("*.meta.json"):
        if "superseded" in meta_path.parts or "failed" in meta_path.parts:
            continue
        on_disk.add(meta_path.name[: -len(".meta.json")])
    indexed = {rec.get("key") for rec in raw.iter_index() if rec.get("key")}
    return {"on_disk": len(on_disk), "indexed": len(indexed),
            "missing_from_index": sorted(on_disk - indexed)[:50],
            "missing_from_index_count": len(on_disk - indexed),
            "stale_index_entries": len(indexed - on_disk)}


def _sharing_modes(cfg) -> dict:
    """Per-thread sharing mode, as recorded by the harvest that fetched it.

    Threads harvested before this was recorded have no entry and fall back to
    the mode on the command line, which is the old behaviour.
    """
    modes: dict = {}
    path = cfg.state_dir / "ledger.jsonl"
    if not path.exists():
        return modes
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("phase") == "harvest" and rec.get("sharing"):
                modes[rec.get("unit")] = rec["sharing"]
    return modes


def run(cfg, raw, progress=None) -> dict:
    """Audit every discovered thread plus the folder graph and the cache itself."""
    thread_records = list(JsonlFile(cfg.state_dir / "threads.jsonl").read_all())
    if not thread_records:
        raise RuntimeError("Nothing to audit: run discover first")

    v1, v2 = _bulk_metadata(raw)
    # Judge each thread by the sharing mode that actually fetched it. Auditing a
    # `--sharing lean` thread as though it were `full` reports a gap that lean
    # will never fill, and an `until audit; do harvest; done` loop whose halves
    # disagree about the flag spins forever making no progress.
    modes = _sharing_modes(cfg)
    results = []
    for i, rec in enumerate(thread_records, 1):
        tid = rec["id"]
        results.append(audit_thread(raw, tid, v1.get(tid), v2.get(tid),
                                    modes.get(tid, cfg.sharing)))
        if progress and i % 200 == 0:
            progress(i, len(thread_records))

    incomplete = [r for r in results if not r.complete]
    by_kind: dict = {}
    for r in incomplete:
        for kind, _detail in r.gaps:
            by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "threads": len(results),
        "complete": len(results) - len(incomplete),
        "incomplete": len(incomplete),
        "refused": len([r for r in results if r.refused]),
        "gaps_by_kind": by_kind,
        "results": results,
        "discovery_gaps": audit_discovery(cfg) + audit_forgotten_threads(cfg),
        "folder_gaps": audit_folders(raw, cfg),
        "non_answers": non_answers(raw),
        "index": index_drift(raw),
        "failed_attempts": raw.failed_attempt_count(),
    }


def report(result: dict) -> str:
    """Markdown, so it can be committed beside the export as evidence."""
    lines = ["# Harvest audit", ""]
    ok = (result["incomplete"] == 0 and not result["folder_gaps"]
          and not result.get("discovery_gaps")
          and not result["non_answers"]
          and not result["index"]["missing_from_index_count"])
    lines.append(f"**{'COMPLETE' if ok else 'INCOMPLETE'}** — "
                 f"{result['complete']}/{result['threads']} threads have everything.")
    lines += ["", "| Check | Result |", "|---|---|"]
    lines.append(f"| threads complete | {result['complete']}/{result['threads']} |")
    lines.append(f"| threads with retryable gaps | {result['incomplete']} |")
    lines.append(f"| threads the server refused (permanent) | {result['refused']} |")
    lines.append(f"| harvested but no longer listed | "
                 f"{sum(1 for k, _ in result.get('discovery_gaps', []) if k == 'forgotten')} |")
    lines.append(f"| discovery completed | "
                 f"{'yes' if not result.get('discovery_gaps') else '**NO**'} |")
    lines.append(f"| unreachable child folders | {len(result['folder_gaps'])} |")
    lines.append(f"| cached non-answers (0/5xx/429) | {len(result['non_answers'])} |")
    lines.append(f"| responses missing from index.jsonl | "
                 f"{result['index']['missing_from_index_count']} |")
    lines.append(f"| failed re-requests kept aside | {result['failed_attempts']} |")

    if result["gaps_by_kind"]:
        lines += ["", "## Gaps by kind", "", "| Kind | Threads |", "|---|---|"]
        for kind, n in sorted(result["gaps_by_kind"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {kind} | {n} |")

    incomplete = [r for r in result["results"] if not r.complete]
    if incomplete:
        lines += ["", "## Incomplete threads", ""]
        for r in incomplete[:200]:
            lines.append(f"- **{r.title[:60] or r.thread_id}** (`{r.thread_id}`)")
            for kind, detail in r.gaps:
                lines.append(f"    - {kind}: {detail}")
        if len(incomplete) > 200:
            lines.append(f"- …and {len(incomplete) - 200} more")

    refused = [r for r in result["results"] if r.refused]
    if refused:
        lines += ["", "## Refused by the server", "",
                  "Asking again will not help: this token cannot read these. "
                  "Recorded so the archive documents its own boundaries, and not "
                  "counted as incomplete.", ""]
        for r in refused[:100]:
            lines.append(f"- **{r.title[:60] or r.thread_id}** (`{r.thread_id}`)")
            for kind, detail in r.refused:
                lines.append(f"    - {kind}: {detail}")

    if result.get("discovery_gaps"):
        lines += ["", "## Discovery", ""]
        lines += [f"- {detail}" for _kind, detail in result["discovery_gaps"]]

    if result["folder_gaps"]:
        lines += ["", "## Folder graph", ""]
        lines += [f"- {detail}" for _kind, detail in result["folder_gaps"][:50]]

    if result["non_answers"]:
        lines += ["", "## Cached non-answers", "",
                  "These requests never got an answer from the server. They are "
                  "re-requested automatically on the next harvest.", ""]
        for canon, status in result["non_answers"][:50]:
            lines.append(f"- `{canon}` → {status}")

    if result["index"]["missing_from_index_count"]:
        lines += ["", "## Index drift", "",
                  f"{result['index']['missing_from_index_count']} responses exist on "
                  "disk but are absent from `index.jsonl`. Run `reindex`.", ""]

    return "\n".join(lines) + "\n"
