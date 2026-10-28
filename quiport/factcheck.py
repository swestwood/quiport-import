"""Fact-check the live Quip API against the assumptions this exporter is built on.

Every assumption in PROBE-FINDINGS.md was measured, not read from the docs -- and
several contradict the docs outright. Before a real export run (especially against
a different or much larger account) re-run this to confirm they still hold.

    python3 -m quiport.factcheck --token "$QUIP_TOKEN" --out export-<id>/_extras/api-probes

Read-only. Discovery is capped so it stays cheap on a huge account.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import quipclient as q

log = logging.getLogger("factcheck")

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class ProbeFailed(RuntimeError):
    """A call this check depended on never answered.

    Distinct from a check *failing*: we learned nothing, rather than learning
    something bad. It matters because factcheck is the gate before a long run,
    and coercing a failed call to `{}` lets a check pass vacuously -- "0 threads
    had a problem" reads exactly like "we never saw any threads". A wrong green
    light here is worse than a red one.
    """


def _answered(resp, what: str):
    """Parsed JSON, or `ProbeFailed` if the server never really answered."""
    if resp is None:
        raise ProbeFailed(f"{what}: no response")
    if not resp.ok:
        if q.is_answer(resp.status):
            # A real answer -- 403/404 -- is information the caller can use.
            return None
        raise ProbeFailed(f"{what}: HTTP {resp.status}")
    try:
        return resp.json()
    except (ValueError, UnicodeDecodeError):
        raise ProbeFailed(f"{what}: response was not readable JSON")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)


class FactCheck:
    def __init__(self, client: q.QuipClient, max_folder_calls=8, max_threads=400):
        self.c = client
        self.max_folder_calls = max_folder_calls
        self.max_threads = max_threads
        self.checks: list[Check] = []
        self.v1: dict = {}
        self.v2: dict = {}
        self.candidates: dict = {}

    def add(self, name, status, detail, **data):
        self.checks.append(Check(name, status, detail, data))
        log.info("[%s] %s -- %s", status, name, detail)

    # -- discovery -------------------------------------------------------

    def discover(self):
        """Walk enough of the folder graph to find representative threads."""
        user = q.current_user(self.c)
        if not user:
            self.add("auth", FAIL, "Could not read /1/users/current -- token invalid?")
            return False
        self.add(
            "auth", PASS,
            f"Authenticated as {user.get('name')} ({user.get('id')}) on {user.get('url')}",
            user_id=user.get("id"), subdomain=user.get("subdomain"),
        )

        # Trash is deliberately excluded everywhere in this project.
        roots = [
            user.get("private_folder_id"), user.get("desktop_folder_id"),
            user.get("archive_folder_id"), user.get("starred_folder_id"),
            *(user.get("shared_folder_ids") or []), *(user.get("group_folder_ids") or []),
        ]
        roots = [r for r in roots if r]

        seen_folders, thread_ids, restricted = {}, [], []
        queue, calls = list(roots), 0
        while queue and calls < self.max_folder_calls and len(thread_ids) < self.max_threads:
            batch = [f for f in queue[:50] if f not in seen_folders]
            queue = queue[50:]
            if not batch:
                continue
            data = _answered(
                self.c.get("/1/folders/", ids=",".join(batch), include_chats="true"),
                f"folder batch of {len(batch)}") or {}
            calls += 1
            for fid, folder in data.items():
                if not isinstance(folder, dict) or "folder" not in folder:
                    continue
                seen_folders[fid] = folder
                for child in folder.get("children", []):
                    if "thread_id" in child:
                        thread_ids.append(child["thread_id"])
                    elif "folder_id" in child:
                        if child.get("restricted"):
                            restricted.append(child["folder_id"])
                        elif child["folder_id"] not in seen_folders:
                            queue.append(child["folder_id"])

        thread_ids = list(dict.fromkeys(thread_ids))[: self.max_threads]
        self.add(
            "discovery", PASS if thread_ids else FAIL,
            f"{len(seen_folders)} folders, {len(thread_ids)} threads, "
            f"{len(restricted)} restricted (sampled, capped)",
            folders=len(seen_folders), threads=len(thread_ids), restricted=restricted,
        )

        # Folder objects carry `link` and `folder_class`, neither documented.
        sample = next(iter(seen_folders.values()), None)
        if sample:
            keys = sorted(sample["folder"].keys())
            has = [k for k in ("link", "folder_class", "folder_type") if k in keys]
            self.add(
                "folder.undocumented_fields",
                PASS if "link" in has else WARN,
                f"folder object exposes {has}; full key set {keys}",
                keys=keys,
            )

        for i in range(0, len(thread_ids), 25):
            batch = thread_ids[i : i + 25]
            self.v1.update(_answered(
                self.c.get("/1/threads/", ids=",".join(batch)),
                f"V1 metadata batch of {len(batch)}") or {})
            self.v2.update(_answered(
                self.c.get("/2/threads/", ids=",".join(batch)),
                f"V2 metadata batch of {len(batch)}") or {})
        return bool(thread_ids)

    def pick_candidates(self):
        """Choose the most informative threads available in *this* account."""
        scored = []
        for tid, t in self.v1.items():
            if "thread" not in t:
                continue
            html = t.get("html") or ""
            scored.append({
                "id": tid,
                "type": t["thread"].get("type"),
                "title": t["thread"].get("title", ""),
                "size": len(html),
                "images": len(re.findall(r"src='/blob/", html)),
                "live_apps": html.count("data-live-app-id"),
                "payloads": html.count("data-live-app-payload"),
                "tables": html.count("<table"),
            })
        if not scored:
            return

        def best(key, **must):
            pool = [s for s in scored if all(s.get(k) == v for k, v in must.items())]
            pool = [s for s in pool if s.get(key)]
            return max(pool, key=lambda s: s[key])["id"] if pool else None

        self.candidates = {
            "largest": max(scored, key=lambda s: s["size"])["id"],
            "with_images": best("images"),
            "with_live_apps": best("live_apps"),
            "with_payload": best("payloads"),
            "table_heavy": best("tables"),
            "spreadsheet": best("size", type="spreadsheet"),
            "slides": best("size", type="slides"),
        }
        self.candidates = {k: v for k, v in self.candidates.items() if v}
        self.add(
            "candidates", PASS, f"selected {len(self.candidates)}: {self.candidates}",
            **self.candidates,
        )

    # -- checks ----------------------------------------------------------

    def check_rate_limit_headers(self):
        resp = self.c.get("/1/users/current")
        present = sorted(h for h in resp.headers if h.lower().startswith(("x-ratelimit", "x-company")))
        lim = self.c.limiter
        self.add(
            "ratelimit.headers", PASS if present else FAIL,
            f"limit={lim.limit} (docs claim 50/min) company_remaining={lim.company_remaining}; "
            f"headers seen: {present}",
            observed_limit=lim.limit, headers=present,
        )

    def check_v2_bulk_shape(self):
        """Docs show V2 bulk fields flat; the API nests them under `thread`."""
        sample = next((v for v in self.v2.values() if isinstance(v, dict)), None)
        if not sample:
            return self.add("v2.bulk_shape", SKIP, "no V2 bulk results")
        nested = "thread" in sample
        self.add(
            "v2.bulk_shape", PASS if nested else FAIL,
            "nested under 'thread' as observed (docs show flat)" if nested
            else "FLAT -- shape changed from what the exporter expects",
            keys=sorted(sample.keys()),
        )

    def check_secret_path(self):
        withsp = [k for k, v in self.v2.items() if (v.get("thread") or {}).get("secret_path")]
        self.add(
            "v2.secret_path", PASS if withsp else FAIL,
            f"{len(withsp)}/{len(self.v2)} V2 records carry secret_path (filenames depend on it)",
        )

    def check_spreadsheet_summary(self):
        summaries = {
            k: (v.get("thread") or {}).get("spreadsheet_summary")
            for k, v in self.v2.items()
            if (v.get("thread") or {}).get("spreadsheet_summary")
        }
        if not summaries:
            return self.add("v2.spreadsheet_summary", WARN, "none found in sample")
        charts = [k for k, s in summaries.items() if s.get("has_charts")]
        formulas = [k for k, s in summaries.items() if s.get("has_formulas")]
        self.add(
            "v2.spreadsheet_summary", PASS,
            f"{len(summaries)} threads; has_charts={len(charts)} has_formulas={len(formulas)} "
            "(drives the needs-attention report)",
            with_charts=charts, with_formulas=formulas,
        )

    def check_annotation_field(self):
        """Docs say `highlighted_section_ids`; the API returns `highlight_section_ids`."""
        documented = observed = 0
        anchors, resolved = 0, 0
        checked = 0
        for tid in list(self.v1)[:60]:
            msgs = _answered(self.c.get(f"/1/messages/{tid}", count=100),
                             f"messages for {tid}")
            if not isinstance(msgs, list) or not msgs:
                continue
            checked += 1
            html, _pages, _ok = q.thread_html_v2(self.c, tid)
            for m in msgs:
                ann = m.get("annotation")
                if not ann:
                    continue
                documented += "highlighted_section_ids" in ann
                observed += "highlight_section_ids" in ann
                for sid in ann.get("highlight_section_ids", []):
                    anchors += 1
                    resolved += sid in html
            if anchors >= 12:
                break

        if not anchors:
            return self.add("messages.annotation_field", SKIP, "no annotated comments in sample")
        self.add(
            "messages.annotation_field", PASS if observed and not documented else FAIL,
            f"'highlight_section_ids' seen {observed}x, documented "
            f"'highlighted_section_ids' seen {documented}x -- code must use the former",
        )
        rate = resolved / anchors
        self.add(
            "messages.anchor_resolution", PASS if rate < 1.0 else WARN,
            f"{resolved}/{anchors} anchors resolve into current HTML ({rate:.0%}); "
            "unanchored comments must be kept, not dropped",
            resolved=resolved, total=anchors,
        )

        vis = []
        for tid in list(self.v1)[:10]:
            chunk = _answered(self.c.get(f"/1/messages/{tid}", count=100),
                              f"messages for {tid}")
            vis += [m.get("visible") for m in (chunk or []) if isinstance(m, dict)]
        self.add(
            "messages.visible_flag", PASS if vis else SKIP,
            f"`visible` present on {sum(1 for v in vis if v is not None)}/{len(vis)} messages "
            "(the deleted-content filter)",
        )

    def check_v1_v2_html(self):
        """The core comparison: V2 is authoritative, V1 supplies live-app names."""
        results = {}
        for label, tid in self.candidates.items():
            v1_html = (self.v1.get(tid) or {}).get("html") or ""
            v2_html, pages, _ok = q.thread_html_v2(self.c, tid)
            if not v2_html:
                continue

            def summaries(h):
                return [s.strip() for s in re.findall(r"<summary>(.*?)</summary>", h, re.S)]

            v1_named = [s for s in summaries(v1_html) if s]
            v2_named = [s for s in summaries(v2_html) if s]
            results[label] = {
                "thread_id": tid, "pages": pages,
                "v1_bytes": len(v1_html), "v2_bytes": len(v2_html),
                "identical": v1_html == v2_html,
                "v1_named_summaries": len(v1_named), "v2_named_summaries": len(v2_named),
                "v1_rel_blob": len(re.findall(r"src='/blob/", v1_html)),
                "v2_abs_blob": len(re.findall(r"src='https://[a-z.]*quip\.com/blob/", v2_html)),
                "v1_br_closed": v1_html.count("<br/>"), "v1_br_bare": len(re.findall(r"<br>", v1_html)),
                "v2_br_closed": v2_html.count("<br/>"), "v2_br_bare": len(re.findall(r"<br>", v2_html)),
                "v1_xml_ok": _xml_ok(v1_html), "v2_xml_ok": _xml_ok(v2_html),
            }

        if not results:
            return self.add("html.v1_vs_v2", SKIP, "no comparable threads")

        # Assumption 1: V1 names live apps, V2 blanks them.
        names = [r for r in results.values() if r["v1_named_summaries"] or r["v2_named_summaries"]]
        if names:
            v1_more = sum(1 for r in names if r["v1_named_summaries"] > r["v2_named_summaries"])
            self.add(
                "html.live_app_names", PASS if v1_more else WARN,
                f"V1 supplies live-app names on {v1_more}/{len(names)} threads where V2 blanks "
                "them -- this is why V1 is still fetched",
            )

        # Assumption 2: blob src form differs between versions.
        blobs = [r for r in results.values() if r["v1_rel_blob"] or r["v2_abs_blob"]]
        if blobs:
            self.add(
                "html.blob_src_form", PASS,
                "V1 emits relative /blob/..., V2 emits absolute https://quip.com/blob/... "
                f"(sampled {len(blobs)} threads) -- extraction must accept both",
            )

        # Assumption 3: neither version is XML-parseable, so use an HTML5 parser.
        bad = [k for k, r in results.items() if not r["v1_xml_ok"] or not r["v2_xml_ok"]]
        self.add(
            "html.not_xml_parseable", PASS if bad else WARN,
            f"{len(bad)}/{len(results)} threads fail strict XML parse in at least one version "
            f"({bad}) -- an HTML5 parser is mandatory",
        )

        # Assumption 4: pagination is rare but must still be followed.
        maxpages = max(r["pages"] for r in results.values())
        biggest = max(r["v2_bytes"] for r in results.values())
        self.add(
            "html.pagination", PASS,
            f"largest sampled document {biggest:,} bytes returned in {maxpages} page(s)",
            max_pages=maxpages, largest_bytes=biggest,
        )
        self.checks.append(Check("html.detail", PASS, "per-thread comparison", results))

    def check_html_differences_openended(self):
        """Diff V1 against V2 without assuming what will differ.

        The targeted checks above assert the differences we already know about,
        which means they stay green if the API changes in some way nobody
        predicted. This one classifies *every* difference and flags any category
        that is not already accounted for -- it is how the known ones were found
        in the first place.
        """
        known, unknown, examined = Counter(), [], 0
        for label, tid in self.candidates.items():
            v1_html = (self.v1.get(tid) or {}).get("html") or ""
            v2_html, _pages, _ok = q.thread_html_v2(self.c, tid)
            if not v1_html or not v2_html or v1_html == v2_html:
                continue
            examined += 1
            for kind, sample in _classify_differences(v1_html, v2_html):
                if kind == "unknown":
                    unknown.append(f"{label}: {sample}")
                else:
                    known[kind] += 1

        if not examined:
            return self.add("html.openended_diff", SKIP, "no differing pairs to compare")
        self.add(
            "html.openended_diff", PASS if not unknown else WARN,
            f"{examined} threads diffed; known categories {dict(known)}; "
            + (f"{len(unknown)} UNEXPECTED difference(s): {unknown[:3]}" if unknown
               else "no unexpected differences"),
            known=dict(known), unknown=unknown[:10],
        )

    def check_parser_roundtrip(self):
        """The project's HTML parser must not lose text on real documents."""
        from . import htmlutil as H

        failures, checked = [], 0
        for label, tid in self.candidates.items():
            html, _pages, _ok = q.thread_html_v2(self.c, tid)
            if not html:
                continue
            checked += 1
            ok, note = H.roundtrip_ok(html)
            if not ok:
                failures.append(f"{label}: {note}")
        if not checked:
            return self.add("parser.roundtrip", SKIP, "nothing to parse")
        self.add(
            "parser.roundtrip", PASS if not failures else FAIL,
            f"{checked} real documents parsed and re-serialized, "
            f"{len(failures)} lost text" + (f": {failures}" if failures else ""),
        )

    def check_corpus_profile(self):
        """What this account actually contains -- for scoping a real run."""
        types, features = Counter(), Counter()
        for tid, t in self.v1.items():
            if "thread" not in t:
                continue
            types[t["thread"].get("type")] += 1
            html = t.get("html") or ""
            if re.search(r"src='/blob/", html):
                features["with_images"] += 1
            if "data-live-app-id" in html:
                features["with_live_apps"] += 1
            if "data-live-app-payload" in html:
                features["with_live_app_payload"] += 1
            if "<table" in html:
                features["with_tables"] += 1
            if not html:
                features["no_body"] += 1
        embedded = sum(
            1 for v in self.v2.values()
            if (v.get("thread") or {}).get("spreadsheet_summary")
            and (v.get("thread") or {}).get("type") != "SPREADSHEET")
        features["embedded_spreadsheets"] = embedded
        self.add("corpus.profile", PASS,
                 f"types {dict(types)}; features {dict(features)}",
                 types=dict(types), features=dict(features))

    def check_blob_headers(self):
        """Blob responses lie about type, so extensions come from alt/magic bytes."""
        tid = self.candidates.get("with_images")
        if not tid:
            return self.add("blob.headers", SKIP, "no thread with images in sample")
        html, _pages, _ok = q.thread_html_v2(self.c, tid)
        refs = re.findall(r"src='(?:https://[a-z.]*quip\.com)?/blob/([\w-]+)/([\w-]+)'", html)
        if not refs:
            return self.add("blob.headers", SKIP, "no blob reference found")

        # Try several: individual blobs can 403/404 independently of the document.
        for thread_id, blob_id in refs[:5]:
            resp = q.blob(self.c, thread_id, blob_id)
            if resp.ok and not resp.body.lstrip().startswith(b'{"error'):
                break
        else:
            return self.add(
                "blob.headers", WARN,
                f"none of {len(refs[:5])} sampled blobs downloaded successfully "
                f"(last status {resp.status}) -- cannot verify header behaviour",
            )

        ctype = resp.headers.get("Content-Type", "")
        disp = resp.headers.get("Content-Disposition")
        magic = resp.body[:8]
        known = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif", b"%PDF": "pdf"}
        sniffed = next((v for sig, v in known.items() if magic.startswith(sig)), None)
        misleading = "form-urlencoded" in ctype or not disp
        self.add(
            "blob.headers", PASS if misleading else WARN,
            f"{len(resp.body):,} bytes, sniffed={sniffed}, Content-Type={ctype!r}, "
            f"Content-Disposition={disp!r} -- "
            + ("headers unreliable as expected; use alt attribute or magic bytes"
               if misleading else "headers look usable, which contradicts PROBE-FINDINGS"),
            sniffed=sniffed, content_type=ctype,
        )

    # -- reporting -------------------------------------------------------

    def run(self):
        """Run every check, recording a call that never answered as a FAIL.

        A probe failure is not a check failure -- it means we learned nothing --
        but it must never be silent. This is the gate before a multi-hour run,
        and a vacuous PASS ("0 threads had a problem" when we saw no threads at
        all) is a worse outcome than any red result.
        """
        try:
            if not self.discover():
                return self.checks
        except ProbeFailed as e:
            self.add("discovery", FAIL,
                     f"could not sample the account: {e}. Nothing below was "
                     "checked -- re-run rather than trusting this result.")
            return self.checks

        self.pick_candidates()
        for check in (
            self.check_rate_limit_headers,
            self.check_v2_bulk_shape,
            self.check_secret_path,
            self.check_spreadsheet_summary,
            self.check_annotation_field,
            self.check_v1_v2_html,
            self.check_html_differences_openended,
            self.check_parser_roundtrip,
            self.check_corpus_profile,
            self.check_blob_headers,
        ):
            name = check.__name__.replace("check_", "")
            try:
                check()
            except ProbeFailed as e:
                self.add(name, FAIL,
                         f"could not be checked -- {e}. This is the API failing "
                         "to answer, not the assumption being wrong.")
        return self.checks

    def report(self) -> str:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        counts = {s: sum(1 for c in self.checks if c.status == s) for s in (PASS, FAIL, WARN, SKIP)}
        lines = [
            "# API fact-check", "",
            f"Run {stamp} against `{self.c.base_url}`.", "",
            f"**{counts[PASS]} pass, {counts[FAIL]} fail, {counts[WARN]} warn, {counts[SKIP]} skipped.**",
            "", "| Check | Result | Detail |", "|---|---|---|",
        ]
        for c in self.checks:
            if c.name == "html.detail":
                continue
            detail = c.detail.replace("|", "\\|")
            lines.append(f"| `{c.name}` | {c.status} | {detail} |")
        lines += ["", f"API calls: {self.c.stats}", ""]
        if counts[FAIL]:
            lines += [
                "## Failures need attention before a real run",
                "A FAIL means the API no longer behaves the way the exporter assumes. "
                "Check PROBE-FINDINGS.md for what the assumption was and why it matters.", "",
            ]
        return "\n".join(lines)


TOKEN_RE = re.compile(r"<[^>]+>|[^<]+")

# Difference categories already understood and handled by the exporter. Anything
# else is reported so a human can decide whether it matters.
BR_RE = re.compile(r"<br\s*/?>", re.I)
BLOB_RE = re.compile(r"/blob/", re.I)
SUMMARY_RE = re.compile(r"</?summary>", re.I)


def _classify_differences(v1_html: str, v2_html: str):
    """Yield (category, sample) for each way the two versions differ."""
    import difflib

    a = TOKEN_RE.findall(v1_html)
    b = TOKEN_RE.findall(v2_html)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left, right = "".join(a[i1:i2]), "".join(b[j1:j2])
        if not left.strip() and not right.strip():
            continue
        # A run of line breaks differing only in self-closing style. Compare the
        # whole differing region, since the diff groups consecutive tokens.
        if not BR_RE.sub("", left).strip() and not BR_RE.sub("", right).strip():
            yield "br_style", ""
        elif BLOB_RE.search(left) or BLOB_RE.search(right):
            yield "blob_url_form", ""
        elif SUMMARY_RE.search(left) or SUMMARY_RE.search(right) or (
                left.strip() and not right.strip()):
            # V1 names the live app in <summary>; V2 leaves it empty.
            yield "live_app_name", ""
        else:
            yield "unknown", f"V1={left[:70]!r} V2={right[:70]!r}"


def _xml_ok(html: str) -> bool:
    try:
        ET.fromstring("<root>" + html + "</root>")
        return True
    except ET.ParseError:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--token", default=os.environ.get("QUIP_TOKEN"), help="or set QUIP_TOKEN")
    p.add_argument("--base-url", default=q.DEFAULT_BASE_URL, help="VPC customers: change this")
    p.add_argument("--out", default="factcheck", help="output directory")
    p.add_argument("--max-folder-calls", type=int, default=8)
    p.add_argument("--max-threads", type=int, default=400)
    args = p.parse_args(argv)

    if not args.token:
        p.error("no token: pass --token or set QUIP_TOKEN")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    client = q.QuipClient(args.token, base_url=args.base_url)
    fc = FactCheck(client, args.max_folder_calls, args.max_threads)
    fc.run()

    (out / "factcheck.md").write_text(fc.report(), encoding="utf-8")
    (out / "factcheck.json").write_text(
        json.dumps([{"name": c.name, "status": c.status, "detail": c.detail, "data": c.data}
                    for c in fc.checks], indent=1),
        encoding="utf-8",
    )
    print("\n" + fc.report())
    print(f"Written to {out}/factcheck.md and {out}/factcheck.json")
    return 1 if any(c.status == FAIL for c in fc.checks) else 0


if __name__ == "__main__":
    sys.exit(main())
