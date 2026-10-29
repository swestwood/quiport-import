"""Command line entry point.

    python3 -m quiport all --export-id myaccount --token "$QUIP_TOKEN"

Phases are separate commands so any of them can be re-run independently.
Everything after `harvest` works offline from the raw cache.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

from . import discover as D
from . import harvest as Hv
from . import linkpass, reports
from . import render as R
from .config import Config
from .quipclient import QuipAuthError, QuipClient
from .rawstore import RawStore
from .state import Checkpoint, JsonlFile, Ledger

log = logging.getLogger("quiport")


def _setup(args, need_token=True, need_export=False):
    cfg = Config.from_args(args)
    if need_token and not cfg.token:
        sys.exit("No token: pass --token or set QUIP_TOKEN")
    if need_export and not cfg.root.exists():
        # Without this a mistyped --export-id creates an empty export and the
        # command cheerfully reports that every document in the account is new.
        existing = sorted(p.name[len("export-"):]
                          for p in cfg.base_dir.glob("export-*") if p.is_dir())
        sys.exit(f"No export at {cfg.root}\n"
                 + (f"Existing exports here: {', '.join(existing)}\n"
                    f"Did you mean --export-id {existing[0]}?" if existing else
                    "Run `discover` and `harvest` first, or pass --base-dir."))
    cfg.ensure_dirs()
    store = RawStore(cfg.raw_dir, retry_errors=cfg.retry_errors)
    ledger = Ledger(cfg.state_dir / "ledger.jsonl")
    client = QuipClient(cfg.token, cfg.base_url, store=store) if cfg.token else None
    return cfg, store, ledger, client


def _discovery_summary(cfg) -> dict:
    path = cfg.state_dir / "discovery.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


# -- commands ------------------------------------------------------------

def cmd_discover(args):
    cfg, store, ledger, client = _setup(args)
    summary = D.discover(client, cfg, ledger)
    (cfg.state_dir / "discovery.json").write_text(json.dumps(summary, indent=1),
                                                  encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def cmd_harvest(args):
    cfg, store, ledger, client = _setup(args)
    started = time.time()

    def progress(i, n, stats):
        rate = i / max(time.time() - started, 1e-6) * 60
        remaining = (n - i) / rate if rate else 0
        print(f"  [{i}/{n}] {stats}  ~{remaining:.0f} min left", flush=True)

    stats = Hv.harvest(client, cfg, ledger, progress=progress,
                       backfill=getattr(args, 'backfill', False))
    print(json.dumps({"harvest": stats, "client": client.stats,
                      "store": store.stats()}, indent=1))
    return 0


def _page_progress():
    """Rendering a large account is a minute or more of silence otherwise."""
    started = time.time()

    def progress(i, n):
        print(f"  rendered {i}/{n} documents ({time.time() - started:.0f}s)",
              flush=True)

    return progress


def cmd_render(args):
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    result = R.render_all(cfg, store, ledger, progress=_page_progress())
    print(json.dumps(result["stats"], indent=1))
    return 0


def cmd_links(args):
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    user_ids = _known_user_ids(store)
    stats = linkpass.run(cfg, user_ids=user_ids)
    print(json.dumps(stats, indent=1))
    return 0


def cmd_reports(args):
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    # write_pages=False: the link pass has already rewritten the documents in
    # place, and re-rendering them here would silently discard that work.
    result = R.render_all(cfg, store, ledger, write_pages=False,
                          progress=_page_progress())
    reports.write_all(cfg, result, _discovery_summary(cfg))
    if args.zip or args.include_raw:
        reports.make_zip(cfg, include_raw=args.include_raw)
    print(json.dumps(result["stats"], indent=1))
    return 0


def cmd_all(args):
    cfg, store, ledger, client = _setup(args)

    if not JsonlFile(cfg.state_dir / "threads.jsonl").exists() or args.rediscover:
        summary = D.discover(client, cfg, ledger)
        (cfg.state_dir / "discovery.json").write_text(json.dumps(summary, indent=1),
                                                      encoding="utf-8")
    started = time.time()

    def progress(i, n, stats):
        rate = i / max(time.time() - started, 1e-6) * 60
        print(f"  [{i}/{n}] {stats}  ~{(n - i) / rate if rate else 0:.0f} min left", flush=True)

    Hv.harvest(client, cfg, ledger, progress=progress,
               backfill=getattr(args, 'backfill', False))

    # Order matters: render writes the documents, the link pass rewrites them in
    # place, and only then are the reports written.
    result = R.render_all(cfg, store, ledger, progress=_page_progress())
    linkpass.run(cfg, user_ids=_known_user_ids(store))
    reports.write_all(cfg, result, _discovery_summary(cfg))
    if args.zip or args.include_raw:
        reports.make_zip(cfg, include_raw=args.include_raw)

    print(json.dumps({"stats": result["stats"], "client": client.stats,
                      "store": store.stats()}, indent=1))
    print(_finished_message(cfg))
    _open_in_browser(cfg.root.resolve() / "index.html")
    return 0


def _finished_message(cfg) -> str:
    """The last thing `all` prints, so it is what the user sees.

    A file:// URL rather than a relative path: most terminals make it
    clickable, and it pastes straight into a browser's address bar. Every path
    and command is built from this run's export id.
    """
    import shlex

    root = cfg.root.resolve()
    export_id = shlex.quote(cfg.export_id)
    return (f"\nSuccess! Your Quip export lives in the {root.name} folder:\n"
            f"  {root}\n\n"
            f"Open this in a browser to check it out:\n"
            f"  {(root / 'index.html').as_uri()}\n\n"
            f"To double-check it, run these two commands:\n"
            f"  python3 -m quiport audit --export-id {export_id}\n"
            f"  python3 -m quiport verify --export-id {export_id}")


def _open_in_browser(page) -> None:
    """Best effort: the export is already written and the link is printed, so a
    machine with no browser (a server, a CI box) just skips this."""
    import webbrowser

    try:
        webbrowser.open(page.as_uri())
    except Exception as e:                  # webbrowser.Error, or a broken $BROWSER
        log.debug("could not open a browser: %s", e)


def cmd_status(args):
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    threads = list(JsonlFile(cfg.state_dir / "threads.jsonl").read_all())
    print(json.dumps({
        "export_id": cfg.export_id, "root": str(cfg.root),
        "discovered_threads": len(threads),
        "ledger": ledger.summary(),
        "raw_store": store.stats(),
        "discovery": _discovery_summary(cfg),
    }, indent=1))
    return 0


# Measured across two real accounts with `--sharing full`: html, messages,
# three sharing calls, ~2.7 blobs and the occasional workbook. The old estimate
# counted only the first five and was 35% low.
CALLS_PER_THREAD = {"full": 7.7, "lean": 4.6}

# Quip enforces two user quotas at once and reports whichever is binding. A
# successful response advertises the per-minute one; the hourly one only shows
# up in the refusal that enforces it. Sustained throughput is the smaller.
USER_CALLS_PER_MINUTE = 50
USER_CALLS_PER_HOUR = 750


def cmd_estimate(args):
    """Project cost before committing to a long run.

    Prefers this export's own measured cost per thread over the table above:
    accounts differ enormously in how many images they carry, and once a few
    dozen threads are harvested the cache knows the real number.
    """
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    threads = list(JsonlFile(cfg.state_dir / "threads.jsonl").read_all())
    n = len(threads)
    if not n:
        sys.exit("Run discover first")

    done = ledger.count("harvest")
    remaining = max(n - done, 0)

    # Bulk metadata is charged per 25-thread window, not per thread.
    overhead = (n // 25) * 2 + 20
    measured = None
    if done >= 25:
        calls_made = store.stats()["calls"]
        measured = round(max(calls_made - overhead, 0) / done, 1)
    per_thread = measured or CALLS_PER_THREAD.get(cfg.sharing, CALLS_PER_THREAD["full"])

    calls_remaining = int(remaining * per_thread)
    # Whichever ceiling binds first over an hour of running.
    per_hour = min(USER_CALLS_PER_MINUTE * 60, USER_CALLS_PER_HOUR)
    hours = calls_remaining / per_hour if per_hour else 0

    print(json.dumps({
        "threads": n, "already_harvested": done, "remaining": remaining,
        "sharing_mode": cfg.sharing,
        "calls_per_thread": per_thread,
        "calls_per_thread_source": "measured from this export" if measured
                                   else "default table (nothing harvested yet)",
        "estimated_calls_remaining": calls_remaining,
        "user_quota_per_minute": USER_CALLS_PER_MINUTE,
        "user_quota_per_hour": USER_CALLS_PER_HOUR,
        "sustained_calls_per_hour": per_hour,
        "estimated_hours_remaining": round(hours, 1),
        "note": ("The hourly quota binds long before the per-minute one: a run "
                 "goes at roughly %d calls/min for ~%d min, then waits out the "
                 "rest of the hour. Elapsed time is mostly waiting, and "
                 "stopping between windows costs nothing."
                 % (USER_CALLS_PER_MINUTE,
                    USER_CALLS_PER_HOUR // USER_CALLS_PER_MINUTE)),
    }, indent=1))
    return 0


def _rebuild(cfg, store, ledger, zip_archive=True):
    """Re-render, re-link and re-report. Offline: costs no API calls.

    Rebuilds the whole export, not just the refreshed documents: a changed
    document moves in `recent.html`, may gain or lose a row in
    `import-notes.html` or `spreadsheets.html`, and changes its own
    "Exported from Quip on ..." footer. Re-rendering everything is how those
    stay consistent, and offline it is cheap.

    The zip is only refreshed if one already exists -- it is a published
    artefact, so leaving a stale copy beside a current export is a trap, but
    creating one nobody asked for is noise.
    """
    result = R.render_all(cfg, store, ledger)
    linkpass.run(cfg, user_ids=_known_user_ids(store))
    reports.write_all(cfg, R.render_all(cfg, store, ledger, write_pages=False),
                      _discovery_summary(cfg))
    archive = cfg.base_dir / f"export-{cfg.export_id}.zip"
    if zip_archive and archive.exists():
        reports.make_zip(cfg)
    return result


def cmd_refresh(args):
    """Re-fetch one or more documents by URL or id, then rebuild."""
    from . import refresh as Rf

    cfg, store, ledger, client = _setup(args, need_export=True)
    tokens = [Rf.parse_target(t) for t in args.targets]
    tokens = [t for t in tokens if t]
    if not tokens:
        sys.exit("No document URL or id given")

    log.info("Refreshing %d document(s)", len(tokens))
    results = Rf.refresh_threads(client, cfg, ledger, tokens)

    for r in results:
        if r.status == Rf.NEW:
            ledger.mark("harvest", r.thread_id, status="done", title=r.title[:80])

    if any(r.changed for r in results) or args.always_rebuild:
        log.info("Rebuilding the export")
        _rebuild(cfg, store, ledger, zip_archive=not args.no_zip)

    print()
    for r in results:
        label = {Rf.NEW: "NEW", Rf.UPDATED: "UPDATED",
                 Rf.UNCHANGED: "unchanged", Rf.FAILED: "FAILED"}[r.status]
        title = r.title or r.token
        print(f"  {label:10} {title}")
        if r.status == Rf.UPDATED:
            print(f"             modified {_usec_date(r.previous_usec)} -> "
                  f"{_usec_date(r.current_usec)}")
        elif r.status == Rf.FAILED:
            print(f"             {r.detail}")
    print(f"\n{client.stats['network']} API calls, "
          f"{store.superseded_count()} superseded response(s) archived")
    return 0 if all(r.status != Rf.FAILED for r in results) else 1


def cmd_check_updates(args):
    """List threads whose updated_usec has moved, then offer to refresh them."""
    from . import refresh as Rf

    cfg, store, ledger, client = _setup(args, need_export=True)
    found = Rf.find_updates(client, cfg, since_usec=args.since)

    id_map = {}
    path = cfg.root / "id-map.json"
    if path.exists():
        id_map = {tid: rec.get("title", "")
                  for tid, rec in (json.loads(path.read_text(encoding="utf-8"))
                                   .get("threads") or {}).items()}

    changed, new, deleted = found["changed"], found["new"], found["deleted"]
    targets = list(changed) + ([] if args.skip_new else list(new))

    print(f"\nChecked {found['checked']} threads.\n")
    if changed:
        print(f"Modified since this export ({len(changed)}):")
        for e in changed:
            print(f"  {Rf.describe(e, id_map)}")
            print(f"      {_usec_date(e['previous_usec'])} -> {_usec_date(e['updated_usec'])}")
    if new and not args.skip_new:
        print(f"\nNot yet in this export ({len(new)}):")
        for e in new:
            print(f"  {Rf.describe(e, id_map)}  [{e.get('type') or 'thread'}]")
    if deleted:
        print(f"\nDeleted in Quip but present here ({len(deleted)}) -- left untouched:")
        for e in deleted:
            print(f"  {Rf.describe(e, id_map)}")

    if not targets:
        print("\nNothing to update.")
        return 0

    # One decision for the whole list, not one per document.
    if args.yes:
        approved = True
    elif not sys.stdin.isatty():
        print(f"\n{len(targets)} document(s) would be refreshed. "
              "Re-run with --yes to apply.")
        return 0
    else:
        answer = input(f"\nRefresh these {len(targets)} document(s)? [y/N] ").strip().lower()
        approved = answer in ("y", "yes")

    if not approved:
        print("Nothing changed.")
        return 0

    results = Rf.refresh_threads(client, cfg, ledger, [e["thread_id"] for e in targets])
    for r in results:
        if r.status == Rf.NEW:
            ledger.mark("harvest", r.thread_id, status="done", title=r.title[:80])
    if any(r.changed for r in results):
        log.info("Rebuilding the export")
        _rebuild(cfg, store, ledger, zip_archive=not args.no_zip)

    updated = sum(1 for r in results if r.changed)
    print(f"\nRefreshed {updated} document(s); {client.stats['network']} API calls.")
    return 0


def _usec_date(usec) -> str:
    from .model import format_export_time
    import datetime as dt
    try:
        return format_export_time(
            dt.datetime.fromtimestamp(int(usec) / 1_000_000,
                                      tz=dt.timezone.utc).isoformat())
    except (TypeError, ValueError, OSError):
        return "unknown"


def cmd_audit(args):
    """Is the raw cache complete? The question `verify` cannot answer."""
    from . import audit as A

    cfg, store, ledger, client = _setup(args, need_token=False, need_export=True)

    def progress(i, n):
        print(f"  audited {i}/{n} threads", flush=True)

    result = A.run(cfg, store, progress=progress)
    text = A.report(result, cfg.export_id)
    (cfg.extras_dir / "audit.md").write_text(text, encoding="utf-8")
    # The report ends with the next steps; nothing printed after it should bury them.
    print(f"\nFull report saved to {cfg.extras_dir / 'audit.md'}\n\n{text}")

    if args.repair:
        import shlex
        base = f"python3 -m quiport {{}} --export-id {shlex.quote(cfg.export_id)}"
        calls = [c for r in result["results"] for c in r.calls_needed]
        if not calls:
            print("Nothing for --repair to fetch: no document is missing anything. "
                  "Follow the other steps above, if any.")
            return 0
        if not cfg.token:
            sys.exit(f"{len(calls)} call(s) to make, which needs your token:\n\n"
                     f"    {base.format('audit')} --repair --token {A.TOKEN_PLACEHOLDER}")
        print(f"\nRepairing: re-queueing {len(calls)} call(s) via harvest.\n")
        # The ledger is what harvest consults, so clearing the affected threads
        # is what makes it fetch them again -- rather than replaying raw calls
        # here and duplicating the per-thread logic.
        incomplete = [r.thread_id for r in result["results"] if not r.complete]
        ledger.reopen("harvest", incomplete)
        client = QuipClient(cfg.token, cfg.base_url, store=store)
        Hv.harvest(client, cfg, ledger)
        print("\nRepair finished. Rebuild the pages, then audit again to confirm:\n\n"
              f"    {base.format('all')} --token {A.TOKEN_PLACEHOLDER}\n"
              f"    {base.format('audit')}")
        return 0

    return 0 if A.is_complete(result) else 1


def cmd_reindex(args):
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    print(json.dumps(store.reindex(), indent=1))
    return 0


def cmd_verify(args):
    from . import verify as V
    cfg, store, ledger, _ = _setup(args, need_token=False, need_export=True)
    checks = V.verify(cfg)
    text = V.report(checks)
    (cfg.extras_dir / "verification.md").write_text(text, encoding="utf-8")
    print("\n" + text)
    print(f"Written to {cfg.extras_dir / 'verification.md'}")
    if not all(c.ok for c in checks):
        return 1
    print("\nAll done, congrats! Enjoy your Quip export.")
    return 0


def cmd_factcheck(args):
    from .factcheck import main as fc_main
    cfg = Config.from_args(args)
    cfg.ensure_dirs()
    return fc_main(["--token", cfg.token, "--base-url", cfg.base_url,
                    "--out", str(cfg.extras_dir / "api-probes")])


def _known_user_ids(store) -> set:
    """User ids, so the link pass can turn person mentions into plain text.

    The docs say an unknown id comes back as "an error message"; it is actually
    a **dict**, `{"error": "Invalid user ID."}`. Accepting any dict as a user
    therefore mistook folder and document ids for people, and the link pass
    turned real document links into plain text. Require a name.
    """
    ids = set()
    for rec in store.iter_index():
        if rec.get("canonical", "").startswith("GET /1/users/") and \
                200 <= rec.get("status", 0) < 300:
            path = store.root / rec["path"]
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(data, dict):
                ids.update(k for k, v in data.items()
                           if isinstance(v, dict) and v.get("name") and "error" not in v)
    return ids


COMMANDS = {
    "factcheck": (cmd_factcheck, "verify the API still behaves as this exporter assumes"),
    "discover": (cmd_discover, "enumerate folders and threads"),
    "harvest": (cmd_harvest, "fetch everything (resumable; the only networked phase)"),
    "render": (cmd_render, "build the HTML tree from the raw cache"),
    "links": (cmd_links, "rewrite Quip links to relative paths"),
    "reports": (cmd_reports, "write reports, index and zip"),
    "all": (cmd_all, "run every phase in order"),
    "refresh": (cmd_refresh, "re-fetch documents by URL or id and rebuild"),
    "check-updates": (cmd_check_updates,
                      "list documents changed in Quip since this export, then offer to refresh"),
    "audit": (cmd_audit, "check the RAW CACHE is complete -- run this after harvesting"),
    "reindex": (cmd_reindex, "rebuild raw/index.jsonl from the files on disk"),
    "verify": (cmd_verify, "check the finished export hangs together and lost no text"),
    "status": (cmd_status, "show progress"),
    "estimate": (cmd_estimate, "project remaining calls and time"),
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="quiport", description="Export a Quip account to a folder of HTML.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (fn, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        Config.add_arguments(p)
        p.set_defaults(func=fn)
        if name in ("refresh", "check-updates"):
            p.add_argument("--no-zip", action="store_true",
                           help="skip refreshing export-<id>.zip after rebuilding")
        if name in ("all", "reports"):
            p.add_argument("--zip", action="store_true",
                           help="also write export-<id>.zip beside the export")
            p.add_argument("--include-raw", action="store_true",
                           help="include the raw API cache in the zip (implies --zip)")
        if name in ("harvest", "all"):
            p.add_argument("--backfill", action="store_true",
                           help="revisit threads already harvested, to pick up "
                                "artefacts this exporter did not fetch before")
        if name == "all":
            p.add_argument("--rediscover", action="store_true",
                           help="re-run discovery even if it has already run")
        if name == "refresh":
            p.add_argument("targets", nargs="+", metavar="URL",
                           help="Quip document URL(s) or thread id(s)")
            p.add_argument("--always-rebuild", action="store_true",
                           help="re-render even when nothing changed")
        if name == "audit":
            p.add_argument("--repair", action="store_true",
                           help="re-queue the incomplete threads and harvest them again")
        if name == "check-updates":
            p.add_argument("--yes", "-y", action="store_true",
                           help="skip the confirmation prompt")
            p.add_argument("--since", type=int, default=0,
                           help="only consider threads modified after this "
                                "microsecond timestamp")
            p.add_argument("--skip-new", action="store_true",
                           help="only refresh documents already in the export")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    try:
        return args.func(args)
    except QuipAuthError:
        # Deliberate stops, not crashes. Several phases now refuse to continue
        # rather than record an incomplete result, and their messages already
        # say what to do -- a traceback on top only obscures that.
        print("\nThe token was rejected (HTTP 401). Nothing further was "
              "fetched; re-run with a valid token to continue.", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"\nStopped: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Everything fetched so far is on disk; re-run to "
              "resume, then `audit` to confirm what is still missing.",
              file=sys.stderr)
        # Returning 130 is not enough. A shell only aborts a loop when the
        # child *died* from SIGINT (WIFSIGNALED); a caught-and-returned 130
        # looks like an ordinary failed command, so
        #     until quiport audit ...; do quiport harvest ...; done
        # starts the next iteration and Ctrl-C can never escape it. Kill
        # ourselves with the signal we caught, which is what the shell is
        # waiting to see.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
        return 130   # only reached where SIGINT cannot kill the process


if __name__ == "__main__":
    sys.exit(main())
