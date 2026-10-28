"""Post-export integrity checks.

Answers the question the archive exists to answer: *can I trust this?*

The important one is `content_preserved`. Everything else checks that the export
hangs together; that check compares the visible text of each rendered page
against the visible text of the raw API response it came from, which is the only
way to prove the pipeline did not quietly drop content between cache and page.

Offline -- reads the export and the raw cache, never the network.

    python3 -m quiport verify --export-id <id>
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from . import htmlutil as H
from .rawstore import RawStore

log = logging.getLogger(__name__)

SKIP_DIRS = {"raw", "state"}
# Additive transforms (checkboxes, live-app labels, comment markers) only add
# words, so any source word absent from the page is real loss. A small tolerance
# absorbs punctuation-splitting differences.
TEXT_COVERAGE_FLOOR = 0.99
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _words(text: str):
    return WORD_RE.findall((text or "").lower())


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    failures: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return "PASS" if self.ok else "FAIL"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(text or "")).strip()


# Markers this exporter adds to the body. They are not source content, and a
# footnote marker fuses onto the preceding word ("internet" + "1"), so they must
# come out before comparing against the raw API text.
GENERATED_CLASSES = ("quip-comment-ref", "quip-liveapp-label",
                     "quip-missing-attachment", "quip-missing-image",
                     "quip-image-placeholder")


def _page_text(html: str, selector_class: str | None = None,
               drop_generated: bool = False) -> str:
    """Visible text of a page, optionally only within one element class."""
    tree = H.parse(html)
    if drop_generated:
        for el in list(H.iter_elements(tree)):
            if any(c in el.classes() for c in GENERATED_CLASSES):
                parent = el.parent
                if isinstance(parent, H.Element):
                    parent.remove(el)
    if selector_class:
        for el in H.iter_elements(tree):
            if selector_class in el.classes():
                return _normalize(el.text())
        return ""
    return _normalize(tree.text())


def _iter_pages(root: Path):
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        yield path, rel.as_posix()


_URI_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")


# -- checks --------------------------------------------------------------

def check_links(root: Path) -> Check:
    """Every relative href and src resolves to a file that exists."""
    broken = []
    total = 0
    for path, rel in _iter_pages(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip scripts: the index page builds hrefs inside JavaScript strings.
        text = re.sub(r"<script.*?</script>", "", text, flags=re.S)
        tree = H.parse(text)
        for el in H.iter_elements(tree):
            for attr in ("href", "src"):
                value = el.get(attr)
                if not value:
                    continue
                value = value.strip()
                # Anything with a URI scheme is somebody else's to resolve.
                # Enumerating schemes missed `tel:` -- 25 phone numbers in one
                # real account were reported as 181 broken links -- and would
                # have missed `sms:`, `callto:` and the rest in turn. The
                # grammar is RFC 3986: letter, then letters/digits/+/-/. then
                # a colon. Safe against local paths because `naming.py` strips
                # every character outside [\w\s-] from a filename, so a
                # generated path can never contain a colon to be mistaken for
                # one -- do not relax that without revisiting this.
                if value.startswith(("#", "//", "/")) or _URI_SCHEME.match(value):
                    continue
                total += 1
                target = (path.parent / htmllib.unescape(value.split("#")[0])).resolve()
                if not target.exists():
                    broken.append(f"{rel} -> {value}")
    return Check("links_resolve", not broken,
                 f"{total} relative links checked, {len(broken)} broken", broken[:25])


def check_id_map(root: Path) -> Check:
    """id-map and the filesystem agree in both directions."""
    path = root / "id-map.json"
    if not path.exists():
        return Check("id_map", False, "id-map.json is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    threads = data.get("threads", {})
    problems = []

    for tid, rec in threads.items():
        if not rec.get("path"):
            problems.append(f"{tid}: no path")
        elif not (root / rec["path"]).exists():
            problems.append(f"{tid}: {rec['path']} does not exist")

    mapped = {rec.get("path") for rec in threads.values()}
    stubs = 0
    orphans = []
    for _path, rel in _iter_pages(root):
        if rel.endswith("index.html") or "/" not in rel:
            continue
        if rel not in mapped:
            page = (root / rel).read_text(encoding="utf-8", errors="replace")
            if "quip-stub" in page:
                stubs += 1
            else:
                orphans.append(rel)
    problems.extend(f"not in id-map: {o}" for o in orphans)

    aliases = data.get("aliases", {})
    dangling = [a for a, tid in aliases.items() if tid not in threads
                and tid not in data.get("folders", {})]
    problems.extend(f"alias points nowhere: {a}" for a in dangling[:10])

    return Check("id_map", not problems,
                 f"{len(threads)} threads, {len(aliases)} aliases, {stubs} stubs, "
                 f"{len(problems)} problems", problems[:25])


def check_content_preserved(root: Path, store: RawStore) -> Check:
    """Every word of the raw API text survives into the rendered page.

    This is the check that matters: it proves nothing was lost between the
    cached response and the page a human will read.

    Word coverage rather than a length ratio, because length alone cannot see a
    *substitution*. An earlier bug replaced date mentions with a longer
    placeholder -- the page grew, so a ratio test stayed green while real
    content was being destroyed.
    """
    id_map_path = root / "id-map.json"
    if not id_map_path.exists():
        return Check("content_preserved", False, "id-map.json is missing")
    threads = json.loads(id_map_path.read_text(encoding="utf-8")).get("threads", {})

    shortfalls, compared, skipped = [], 0, 0
    for tid, rec in threads.items():
        page_path = root / (rec.get("path") or "")
        if not page_path.exists():
            continue
        source_html = store.load_thread_html(tid)
        if not source_html.strip():
            skipped += 1                      # chats and empty documents
            continue
        source_text = _normalize(H.parse(source_html).text())
        if not source_text:
            skipped += 1
            continue
        rendered_text = _page_text(
            page_path.read_text(encoding="utf-8", errors="replace"),
            "quip-body", drop_generated=True)
        compared += 1

        source_words = Counter(_words(source_text))
        rendered_words = Counter(_words(rendered_text))
        if not source_words:
            continue
        missing = source_words - rendered_words
        total = sum(source_words.values())
        coverage = 1.0 - (sum(missing.values()) / total)
        if coverage < TEXT_COVERAGE_FLOOR:
            lost = ", ".join(w for w, _ in missing.most_common(6))
            shortfalls.append(
                f"{rec.get('title', tid)[:40]} ({tid}): {coverage:.1%} of words kept, "
                f"{sum(missing.values())}/{total} missing e.g. {lost}")

    return Check("content_preserved", not shortfalls,
                 f"{compared} documents compared word-by-word against raw "
                 f"({skipped} with no body), {len(shortfalls)} lost text", shortfalls[:25])


def check_metadata(root: Path) -> Check:
    """Every document page carries a machine-readable metadata block."""
    missing = []
    checked = 0
    for path, rel in _iter_pages(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "quip-stub" in text or rel.endswith("index.html") or "/" not in rel:
            continue
        checked += 1
        if "data-quip-thread-id=" not in text:
            missing.append(rel)
    return Check("metadata_present", not missing,
                 f"{checked} document pages, {len(missing)} without a thread id",
                 missing[:25])


def check_blobs(root: Path) -> Check:
    """Referenced files exist; downloaded files are referenced."""
    files_dir = root / "Imported from Quip" / "_files"
    # Dotfiles are the operating system's, not ours -- .DS_Store was showing up
    # as an unreferenced blob.
    on_disk = ({p.resolve() for p in files_dir.rglob("*")
                if p.is_file() and not p.name.startswith(".")}
               if files_dir.exists() else set())

    referenced, broken = set(), []
    for path, rel in _iter_pages(root):
        tree = H.parse(path.read_text(encoding="utf-8", errors="replace"))
        for el in H.iter_elements(tree):
            src = el.get("src") or el.get("href") or ""
            if not src or src.startswith(("http:", "https:", "data:", "#", "/")):
                continue
            if "_files/" not in src:
                continue
            target = (path.parent / htmllib.unescape(src)).resolve()
            referenced.add(target)
            if not target.exists():
                broken.append(f"{rel} -> {src}")

    unreferenced = on_disk - referenced
    problems = broken + [f"downloaded but unreferenced: {p.name}" for p in sorted(unreferenced)[:10]]
    return Check("blobs", not broken,          # unreferenced is informational, not a failure
                 f"{len(on_disk)} files on disk, {len(referenced)} referenced, "
                 f"{len(broken)} broken, {len(unreferenced)} unreferenced",
                 problems[:25])


ICON_CELL_RE = re.compile(
    r'<span class="quip-title-cell"><svg[^>]*><use href="#quip-icon-([\w-]+)"></use>'
    r'</svg><a href="([^"]+)"')
SYMBOL_RE = re.compile(r'<symbol id="([^"]+)"')
USE_RE = re.compile(r'<use href="#([^"]+)"')


def check_icons(root: Path) -> Check:
    """Every type icon matches the thread it labels, and resolves to a symbol.

    Two ways this can go wrong silently: an icon that misdescribes the row (a
    document shown as a spreadsheet because it appears in the spreadsheet
    report), and a `<use>` pointing at a symbol the page never defined, which
    renders as blank space rather than an error.
    """
    id_map_path = root / "id-map.json"
    if not id_map_path.exists():
        return Check("icons", False, "id-map.json is missing")
    id_map = json.loads(id_map_path.read_text(encoding="utf-8"))
    type_by_path = {r["path"]: r.get("type") for r in id_map.get("threads", {}).values()}

    problems, checked, dangling_pages = [], 0, 0
    for path, rel in _iter_pages(root):
        html = path.read_text(encoding="utf-8", errors="replace")

        defined = set(SYMBOL_RE.findall(html))
        used = set(USE_RE.findall(html))
        missing = used - defined
        if missing:
            dangling_pages += 1
            problems.append(f"{rel}: <use> with no symbol: {sorted(missing)}")

        for icon, href in ICON_CELL_RE.findall(html):
            target = unquote(href.split("#")[0])
            resolved = (path.parent / target).resolve()
            try:
                key = resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            actual = type_by_path.get(key)
            if actual is None:
                continue                      # folder rows and non-thread targets
            checked += 1
            if actual != icon:
                problems.append(f"{rel}: icon '{icon}' but {key} is a {actual}")

    return Check("icons", not problems,
                 f"{checked} title icons cross-checked against thread types, "
                 f"{dangling_pages} pages with dangling symbol refs, "
                 f"{len(problems)} problems", problems[:25])


def check_index_complete(root: Path) -> Check:
    """Every document is reachable by browsing index.html, not just by search.

    The tree is built by walking folders, so a thread no folder lists falls out
    of it while still existing on disk, in id-map.json and in the inlined search
    index -- which is how one went missing after `refresh` added it. Nothing
    else notices: the file is present and every link resolves.

    The search JSON is stripped before looking, because it mentions every
    document and would mask exactly the failure this is here to catch.
    """
    index_path = root / "index.html"
    id_map_path = root / "id-map.json"
    if not index_path.exists() or not id_map_path.exists():
        return Check("index_complete", False, "index.html or id-map.json is missing")

    html = re.sub(r"<script.*?</script>", "", index_path.read_text(encoding="utf-8",
                                                                  errors="replace"), flags=re.S)
    linked = set()
    for el in H.iter_elements(H.parse(html), "a"):
        href = (el.get("href") or "").split("#")[0]
        if href:
            linked.add(unquote(htmllib.unescape(href)))

    threads = json.loads(id_map_path.read_text(encoding="utf-8")).get("threads", {})
    missing = sorted(f"{rec.get('title', tid)[:40]} ({tid}) -> {rec.get('path')}"
                     for tid, rec in threads.items()
                     if rec.get("path") and rec["path"] not in linked)
    return Check("index_complete", not missing,
                 f"{len(threads)} threads, {len(missing)} not browsable from index.html",
                 missing[:25])


REMOTE_SRC_RE = re.compile(r'\b(?:src|srcset)\s*=\s*"(https?:)?//', re.I)
REMOTE_LINK_RE = re.compile(r'<link[^>]+href\s*=\s*"(https?:)?//', re.I)


def check_offline(root: Path) -> Check:
    """No page fetches anything off this machine when it is opened.

    Every embedded resource -- images, attachments, stylesheet, scripts -- must
    be a local file. A single remote `src` would mean opening an archived
    document silently announces to a third party that it was opened, and from
    which IP, years after the account is gone. Links a reader *clicks* are a
    different matter and are left alone.
    """
    offenders = []
    for path, rel in _iter_pages(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for rx, what in ((REMOTE_SRC_RE, "src"), (REMOTE_LINK_RE, "link")):
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{rel}:{line} remote {what}")
    return Check("offline", not offenders,
                 f"no page loads a remote resource ({len(offenders)} found)",
                 offenders[:25])


def check_reports(root: Path) -> Check:
    expected = ["index.html", "recent.html", "needs-attention.html",
                "spreadsheets.html", "slide-decks.html", "limitations.html",
                "quip-export.css", "id-map.json"]
    missing = [name for name in expected if not (root / name).exists()]
    return Check("reports_present", not missing,
                 f"{len(expected) - len(missing)}/{len(expected)} present", missing)


# -- driver --------------------------------------------------------------

def verify(cfg) -> list[Check]:
    root = cfg.root
    if not root.exists():
        raise RuntimeError(f"No export at {root}")
    store = RawStore(cfg.raw_dir)
    checks = [
        check_reports(root),
        check_id_map(root),
        check_links(root),
        check_blobs(root),
        check_metadata(root),
        check_icons(root),
        check_index_complete(root),
        check_offline(root),
        check_content_preserved(root, store),
    ]
    for c in checks:
        log.info("[%s] %s -- %s", c.status, c.name, c.detail)
        for f in c.failures:
            log.info("        %s", f)
    return checks


def report(checks: list[Check]) -> str:
    lines = ["# Export verification", ""]
    passed = sum(1 for c in checks if c.ok)
    lines.append(f"**{passed}/{len(checks)} checks passed.**")
    lines += ["", "| Check | Result | Detail |", "|---|---|---|"]
    for c in checks:
        lines.append(f"| `{c.name}` | {c.status} | {c.detail} |")
    for c in checks:
        if c.failures:
            lines += ["", f"## {c.name}", ""]
            lines += [f"- {f}" for f in c.failures]
    return "\n".join(lines) + "\n"
