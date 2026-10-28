"""Render one thread to a standalone HTML page."""

from __future__ import annotations

import logging

from .. import htmlutil as H
from ..model import Thread, format_date
from ..naming import path_to_root, relative_path
from . import comments as C
from . import transform as T
from .templates import (HIGHLIGHT_NAME, SEARCH_INDEX_NAME, STYLESHEET_NAME,
                        UTILS_NAME, site_chrome,
                        breadcrumb, footer, icon_sprite, page)

log = logging.getLogger(__name__)


class RenderReport:
    """Collects everything lossy or surprising about one document."""

    def __init__(self, thread_id: str, title: str):
        self.thread_id = thread_id
        self.title = title
        self.entries: list[dict] = []

    def add(self, kind: str, **data):
        self.entries.append({"kind": kind, **data})

    def kinds(self) -> set:
        return {e["kind"] for e in self.entries}

    def has(self, kind: str) -> bool:
        return any(e["kind"] == kind for e in self.entries)

    def count(self, kind: str) -> int:
        return sum(1 for e in self.entries if e["kind"] == kind)


def render_document(thread: Thread, ctx) -> tuple[str, RenderReport]:
    """Build the full page for a document or spreadsheet thread."""
    report = RenderReport(thread.id, thread.display_title)
    page_path = thread.output_path
    tree = H.parse(thread.html or "")

    if thread.html:
        ok, note = H.roundtrip_ok(thread.html)
        if not ok:
            report.add("parse_warning", detail=note)
            log.warning("  %s: round-trip mismatch (%s)", thread.id, note)
    else:
        report.add("no_content")

    # A prefix of a document is worse than an obvious gap, because it reads as
    # the whole thing. Say so on the page as well as in the reports.
    if not thread.html_complete:
        report.add("incomplete_html", pages=thread.html_pages,
                   detail=next((e for e in thread.errors if "incomplete HTML" in e), ""))

    # -- content transforms ---------------------------------------------
    blob_resolver = ctx.blobs.resolver_for(thread)
    T.rewrite_blobs(tree, blob_resolver, page_path, report)
    # After rewrite_blobs, so an unfetchable image is already marked missing and
    # only genuinely empty wells are left to shrink.
    placeholders = T.render_image_placeholders(tree, report)
    attachments, dated = T.render_controls(
        tree, report, thread.created_usec or thread.updated_usec)
    T.tag_lists(tree)
    checked = T.render_checklists(tree)
    tables = T.tag_tables(tree)
    dark_cells = T.apply_readable_cell_text(tree)
    T.tag_code_blocks(tree)
    T.strip_empty_anchors(tree)

    live_names = T.live_app_names_from_v1(thread.v1_html)
    live_apps = T.render_live_apps(tree, live_names, report)
    if live_apps:
        # One summary entry per document, so the reports can list live apps
        # without re-deriving them from individual entries.
        report.add("live_apps", count=len(live_apps),
                   names=sorted({a["name"] for a in live_apps}),
                   empty=sum(1 for a in live_apps if not a["has_dump"]),
                   recovered=sum(1 for a in live_apps if a["payload_added"]))

    # -- comments --------------------------------------------------------
    comment_set = C.build(thread.messages, tree, report)
    C.insert_markers(tree, comment_set)

    body_html = H.serialize(tree)

    # -- assemble --------------------------------------------------------
    css = relative_path(page_path, STYLESHEET_NAME)
    crumbs = _breadcrumbs(thread, ctx, page_path)
    meta = render_metadata(thread, ctx, page_path)

    # Only the symbols this page actually references: an unused sprite is dead
    # weight, and a missing one renders as blank space. `verify` checks that
    # every <use> resolves, which is what keeps these two lists honest.
    wanted = []
    if comment_set.anchored:
        wanted.append("comment")
    if "prettyprint" in (thread.html or ""):
        wanted.append("code")           # the language switcher's trigger
    sprite = icon_sprite(*wanted) if wanted else ""

    truncation = ""
    if not thread.html_complete:
        truncation = (
            '<p class="quip-missing-image quip-truncated">This document is '
            "<strong>incomplete</strong>: the export holds only the first "
            f"{thread.html_pages} page(s) of its content. Re-run "
            "<code>harvest</code> and then <code>audit</code> to complete it.</p>")

    # Chrome first, and outside `.quip-body` -- `verify`'s content check reads
    # that element and would compare the nav's words against the document's.
    parts = [sprite, site_chrome(page_path), crumbs, meta, truncation,
             _xlsx_banner(thread, ctx, page_path, report),
             f'<article class="quip-body" id="quip-document">{body_html}</article>',
             C.render_comments(comment_set, blob_resolver, page_path, ctx.user_name),
             C.render_chat(comment_set, blob_resolver, page_path,
                           name_resolver=ctx.user_name)]

    report.add("stats", tables=tables, checkboxes=checked,
               live_apps=len(live_apps), comments=comment_set.total_groups,
               chat_messages=len(comment_set.chat), attachments=attachments,
               dates_dated=dated, image_placeholders=placeholders,
               dark_cells=dark_cells)

    html = page(
        thread.display_title,
        "\n".join(p for p in parts if p),
        css,
        body_class="quip-document",
        extra_head=_scripts(page_path),
        footer=footer(ctx.exported_at(thread.id)),
    )
    return html, report


def _scripts(page_path: str) -> str:
    """Syntax highlighting, loaded only where it can be used.

    `defer` so neither script blocks the document rendering: the page is
    readable immediately and the colours arrive a moment later. Both files are
    vendored at the export root, because the archive has to work from a file://
    URL long after any CDN would have stopped serving them.
    """
    root = path_to_root(page_path)
    return (f'<script src="{H.escape_attr(root + HIGHLIGHT_NAME)}" defer></script>\n'
            f'<script src="{H.escape_attr(root + SEARCH_INDEX_NAME)}" defer></script>\n'
            f'<script src="{H.escape_attr(root + UTILS_NAME)}" defer></script>\n')


def _xlsx_banner(thread: Thread, ctx, page_path: str, report) -> str:
    """Offer the real workbook, for threads that have one.

    The tables below are Quip's HTML flattening: one anonymous table per sheet,
    every cell a string. The workbook keeps cell types, number formats, column
    widths and the sheets as separate sheets -- so for anyone who wants to
    *use* the data rather than read it, this is the copy that matters.
    """
    if not thread.spreadsheet_summary:
        return ""
    rel = ctx.blobs.materialize_xlsx(thread, page_path)
    if not rel:
        report.add("xlsx_missing")
        return ('<p class="quip-xlsx quip-xlsx-missing">Quip returned no spreadsheet '
                "export for this document, so only the flattened tables below "
                "survive.</p>")

    s = thread.spreadsheet_summary or {}
    detail = []
    if s.get("num_tables"):
        n = s["num_tables"]
        detail.append(f"{n} sheet{'s' if n != 1 else ''}")
    if s.get("has_formulas"):
        # Measured: Quip's own exporter writes no <f> elements, so formulas
        # arrive as their computed values in both formats. Say so rather than
        # implying the workbook restores them.
        detail.append("formulas as computed values")
    if s.get("has_charts"):
        detail.append("charts not exported")
    suffix = f" &middot; {', '.join(detail)}" if detail else ""

    report.add("xlsx_exported", path=rel)
    return (f'<p class="quip-xlsx"><a href="{H.escape_attr(rel)}" '
            f'class="quip-xlsx-link" download>Download spreadsheet (.xlsx)</a> '
            f"&mdash; cell types, formatting and separate sheets, which the tables "
            f"below cannot carry{suffix}.</p>")


def render_metadata(thread: Thread, ctx, page_path: str) -> str:
    """The quote-style header block.

    Values are duplicated into data-* attributes so a future tool can read them
    back without parsing prose.
    """
    people = ctx.describe_sharing(thread)
    folders = thread.folder_paths or []

    rows = [
        ("Created", format_date(thread.created_usec)),
        ("Last modified", format_date(thread.updated_usec)),
    ]

    link_html = ""
    if thread.link:
        link_html = f'<a href="{H.escape_attr(thread.link)}" class="quip-source-link">' \
                    f'{H.escape_text(thread.link)}</a>'

    attrs = {
        "class": "quip-meta",
        "data-quip-thread-id": thread.id,
        "data-quip-secret-path": thread.secret_path,
        "data-quip-type": thread.type,
        "data-quip-created-usec": str(thread.created_usec),
        "data-quip-updated-usec": str(thread.updated_usec),
        "data-quip-link": thread.link,
        "data-quip-exported-at": ctx.raw.fetched_at(
            "GET", f"/2/threads/{thread.id}/html", {}) or ctx.harvested_at,
    }
    if thread.thread_class:
        attrs["data-quip-thread-class"] = thread.thread_class
    attr_str = " ".join(f'{k}="{H.escape_attr(v)}"' for k, v in attrs.items() if v)

    lines = [f"<blockquote {attr_str}>"]
    for label, value in rows:
        lines.append(f'<div><span class="quip-meta-label">{label}:</span> '
                     f'{H.escape_text(value)}</div>')
    if link_html:
        lines.append(f'<div><span class="quip-meta-label">Imported from:</span> {link_html}</div>')
    lines.append(f'<div><span class="quip-meta-label">Access:</span> {people}</div>')

    author = ctx.user_name(thread.author_id)
    if author:
        lines.append(f'<div><span class="quip-meta-label">Author:</span> '
                     f'{H.escape_text(author)}</div>')

    if folders:
        links = []
        for fpath in folders:
            index = f"{fpath}/index.html"
            links.append(f'<a href="{H.escape_attr(relative_path(page_path, index))}">'
                         f'{H.escape_text(fpath.split("/")[-1])}</a>')
        label = "Folder" if len(links) == 1 else "Folders"
        lines.append(f'<div><span class="quip-meta-label">{label}:</span> '
                     f'{", ".join(links)}</div>')

    lines.append(f'<div class="quip-meta-ids"><span class="quip-meta-label">Quip ID:</span> '
                 f'{H.escape_text(thread.id)}'
                 + (f' &middot; secret path {H.escape_text(thread.secret_path)}'
                    if thread.secret_path else "") + "</div>")
    lines.append("</blockquote>")
    return "\n".join(lines)


def _breadcrumbs(thread: Thread, ctx, page_path: str):
    home = relative_path(page_path, "index.html")
    parts = [("All documents", home)]
    if thread.folder_paths:
        crumb = thread.folder_paths[0].split("/")
        acc = []
        for seg in crumb:
            acc.append(seg)
            path = "/".join(acc)
            if path == ctx.content_dir_name:
                continue
            parts.append((seg, relative_path(page_path, f"{path}/index.html")))
    parts.append((thread.display_title, None))
    return breadcrumb(parts)


def render_stub(thread: Thread, stub_path: str, canonical_path: str, ctx) -> str:
    """A pointer page for the other folders a thread appears in.

    Quip folders are tags, so one thread can live in several places. Rather
    than duplicating the file, every non-canonical location links to the real
    one.
    """
    target = relative_path(stub_path, canonical_path)
    css = relative_path(stub_path, STYLESHEET_NAME)
    body = f"""<div class="quip-stub">
<h1>{H.escape_text(thread.display_title)}</h1>
<p>This document is filed in more than one Quip folder. The full copy lives at:</p>
<p><a href="{H.escape_attr(target)}">{H.escape_text(canonical_path)}</a></p>
<p class="quip-meta-ids">Quip ID {H.escape_text(thread.id)}
{f"&middot; secret path {H.escape_text(thread.secret_path)}" if thread.secret_path else ""}</p>
</div>"""
    return page(f"{thread.display_title} (shortcut)", body, css,
                body_class="quip-stub-page", footer=footer(ctx.exported_at(thread.id)))
