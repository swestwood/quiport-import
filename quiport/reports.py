"""Phase 5 -- the reports, the browsable index, and the zip archive.

Reports exist so nothing is lost silently: anything the renderer could not
carry over faithfully is enumerated somewhere a human can act on.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import zipfile
from collections import Counter
from pathlib import Path

from . import htmlutil as H
from .model import format_date
from .naming import relative_path
from .render.templates import (ICON_BY_KIND, IMPORT_NOTES, SEARCH_INDEX_NAME,
                               STYLESHEET_NAME, UTILS_NAME, chrome_scripts, footer,
                               site_chrome, icon_sprite, icon_use, page)

log = logging.getLogger(__name__)


def _table(headers, rows, class_name="quip-report-table") -> str:
    head = "".join(f"<th>{H.escape_text(h)}</th>" for h in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    return (f'<div class="quip-report"><table class="{class_name}">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")


def _link(page_path: str, target: str, label: str) -> str:
    if not target:
        return H.escape_text(label)
    return f'<a href="{H.escape_attr(relative_path(page_path, target))}">{H.escape_text(label)}</a>'


def _title_cell(page_path: str, thread) -> str:
    """Type icon plus the linked title, for a report table.

    The icon follows the thread's own type, not the report it appears in: a
    document with an embedded spreadsheet is still a document, and showing it
    as a spreadsheet in `spreadsheets.html` would misdescribe it.
    """
    return ('<span class="quip-title-cell">' + icon_use(thread.type)
            + _link(page_path, thread.output_path, thread.display_title) + "</span>")


def _icon_label(kind: str, label: str) -> str:
    """Icon plus plain text, for rows that have no local page to link to."""
    return (f'<span class="quip-title-cell">{icon_use(kind)}'
            f"{H.escape_text(label)}</span>")


def unknown_icon_kinds(threads) -> set:
    """Thread types with no icon of their own.

    Anything here silently renders as a document, which would be wrong rather
    than merely plain -- so it is surfaced instead of absorbed.
    """
    return {t.type for t in threads.values() if t.type not in ICON_BY_KIND}


def _xlsx_cell(page_path: str, thread) -> str:
    """Link the exported workbook the renderer wrote, if there is one."""
    if not thread.xlsx_path:
        return "—"
    return (f'<a href="{H.escape_attr(relative_path(page_path, thread.xlsx_path))}" '
            f"download>.xlsx</a>")


def _quip_link(url: str) -> str:
    """A link back to the original in Quip.

    Tagged `quip-source-link` so the link pass leaves it pointing at Quip --
    the whole point of the column is that it is *not* the local copy.
    """
    return f'<a href="{H.escape_attr(url)}" class="quip-source-link">Quip</a>' if url else ""


def _folder_of(thread) -> str:
    return thread.folder_paths[0] if thread.folder_paths else ""


# -- individual reports --------------------------------------------------

def _folder_cell(page_path: str, thread) -> str:
    """The containing folder, linked to its local index page.

    Shown without the "Imported from Quip/" prefix -- it is the same on every
    row, so repeating it is noise.
    """
    path = _folder_of(thread)
    if not path:
        return "—"
    label = path.split("/", 1)[1] if "/" in path else path
    return _link(page_path, f"{path}/index.html", label)


def sharing_label(thread) -> str:
    """The compact "Private" / "8 people" / "Company" of Quip's own recents.

    Deliberately *not* built from V1's `expanded_user_ids`. That field expands
    company-wide sharing into every user who could reach the document -- one
    thread on a real account listed 7,127 -- which is true but useless as a
    label, and reads as though a private note were shared with the world. The
    V2 members endpoint reports the 103 people actually on that thread, and
    `sharing.company_mode` says the rest in one word.
    """
    if (thread.sharing or {}).get("company_mode"):
        return "Company"
    ids = {u for u in (thread.member_ids or thread.expanded_user_ids or []) if u}
    if len(ids) <= 1:
        return "Private"
    return f"{len(ids)} person" if len(ids) == 1 else f"{len(ids)} people"


def _short_date(usec) -> str:
    """"Jun 23, 2016" -- narrow enough for two date columns side by side."""
    if not usec:
        return "—"
    return dt.datetime.fromtimestamp(usec / 1e6, dt.timezone.utc).strftime("%b %-d, %Y")


def _day_heading(usec, today=None) -> str:
    """"September 10, 2025" -- always with the year.

    Quip omits the year for the current one, but Quip is a live product being
    read today. This is an archive: "October 17" is ambiguous the moment the
    export outlives the year it was taken in, and there is no cost to saying so.

    No "Today"/"Yesterday" either, for the same reason -- a heading that
    silently becomes a lie is worse than a plain date.
    """
    d = dt.datetime.fromtimestamp((usec or 0) / 1e6, dt.timezone.utc)
    return d.strftime("%B %-d, %Y")


def recent(cfg, threads) -> str:
    """Everything by last-modified, newest first, grouped under day headings."""
    page_path = "recent.html"
    items = sorted(threads.values(), key=lambda t: t.updated_usec or 0, reverse=True)
    today = dt.datetime.now(dt.timezone.utc)

    groups, current, rows = [], None, []
    for t in items:
        day = dt.datetime.fromtimestamp((t.updated_usec or 0) / 1e6,
                                        dt.timezone.utc).date()
        if day != current:
            if rows:
                groups.append((current, rows))
            current, rows = day, []
        rows.append(t)
    if rows:
        groups.append((current, rows))

    out = []
    for day, members in groups:
        heading = _day_heading(int(dt.datetime.combine(
            day, dt.time(), dt.timezone.utc).timestamp() * 1e6))
        out.append('<section class="quip-day-group">')
        out.append(f'<h2 class="quip-day">{H.escape_text(heading)}</h2>')
        out.append('<ul class="quip-recent-list">')
        for t in members:
            href = relative_path(page_path, t.output_path)
            when = (f'<span class="quip-when quip-recent-when" '
                    f'data-quip-when="recent" '
                    f'data-quip-updated="{int(t.updated_usec or 0)}">'
                    f'{H.escape_text(_short_date(t.updated_usec))}</span>')
            # Static: a creation date is a fixed fact, and it is the column the
            # filter acts on, so it must be readable with scripting off.
            created = (f'<span class="quip-recent-created">'
                       f'{H.escape_text(_short_date(t.created_usec))}</span>')
            # The filter reads `data-quip-created` off the row.
            out.append(
                f'<li class="quip-recent-item" '
                f'data-quip-created="{int(t.created_usec or 0)}">'
                '<a class="quip-entry quip-recent-row" '
                f'href="{H.escape_attr(href)}">'
                + icon_use(t.type)
                + f'<span class="quip-entry-title">{H.escape_text(t.display_title)}</span>'
                + f'<span class="quip-recent-share">{H.escape_text(sharing_label(t))}</span>'
                + created + when + "</a></li>")
        out.append("</ul></section>")

    # Some accounts carry documents whose `updated_usec` was moved by something
    # other than a person editing them -- on one real account 165 documents
    # created before 2020 all changed within the same week -- which pushes very
    # old material to the top of a list meant to show recent work. Filtering on
    # the creation date is the way back, since that timestamp does not move.
    controls = (
        '<form class="quip-recent-filter" id="quip-recent-filter" '
        'onsubmit="return false">'
        '<label for="quip-created-after">Hide documents created before</label> '
        '<input type="date" id="quip-created-after" name="created-after"> '
        '<button type="button" id="quip-created-clear">Clear</button> '
        '<span class="quip-filter-status" id="quip-filter-status" '
        'role="status"></span>'
        "</form>")
    header = ('<div class="quip-recent-head" aria-hidden="true">'
              '<span class="quip-entry-title"></span>'
              '<span class="quip-recent-share">Members</span>'
              '<span class="quip-recent-created">Created</span>'
              '<span class="quip-recent-when">Updated</span></div>')

    body = (icon_sprite() + site_chrome(page_path, "recent.html")
            + "<h1>Most recently modified</h1>"
            f'<p class="quip-recent-count">{len(items)} threads, '
            "most recently modified first.</p>"
            + controls + header + "\n".join(out))
    # Report pages carried no script at all, so the relative dates never
    # rendered and the filter never initialised. Only `index.html` had one.
    return page("Most recently modified", body, STYLESHEET_NAME,
                body_class="quip-report-page",
                extra_head=chrome_scripts(page_path), footer=footer())


def spreadsheets(cfg, threads) -> str:
    """Standalone and embedded spreadsheets, ranked by how lossy they are.

    A document carrying `spreadsheet_summary` has a real spreadsheet inside it
    that the HTML flattens to a plain <table>. In the test account 33 of 48 such
    threads were ordinary documents, so this is the common case, not the edge.
    """
    page_path = "spreadsheets.html"
    with_summary = [t for t in threads.values() if t.spreadsheet_summary]
    with_summary.sort(key=lambda t: (not t.has_charts, not t.has_formulas,
                                     t.display_title.lower()))

    rows = []
    exported = 0
    for t in with_summary:
        s = t.spreadsheet_summary or {}
        flags = []
        if t.has_charts:
            flags.append('<span class="quip-flag">charts lost</span>')
        if t.has_formulas:
            flags.append('<span class="quip-flag">formulas as values</span>')
        xlsx = _xlsx_cell(page_path, t)
        if xlsx != "—":
            exported += 1
        rows.append([
            _title_cell(page_path, t),
            H.escape_text(_folder_of(t)),
            "standalone" if t.is_spreadsheet else "embedded in document",
            str(s.get("num_tables", "")), str(s.get("max_rows", "")),
            str(s.get("max_cols", "")), " ".join(flags) or "—",
            xlsx,
            _quip_link(t.link),
        ])

    # Table-heavy documents with no spreadsheet_summary: a weaker signal, but
    # still worth a human glance.
    heavy = []
    for t in threads.values():
        if t.spreadsheet_summary or not t.html:
            continue
        n = t.html.count("<table")
        if n >= 3:
            heavy.append((n, t))
    heavy.sort(key=lambda p: -p[0])
    heavy_rows = [[
        _title_cell(page_path, t),
        H.escape_text(_folder_of(t)), str(n), _quip_link(t.link),
    ] for n, t in heavy]

    embedded = sum(1 for t in with_summary if not t.is_spreadsheet)
    body = [
        icon_sprite(), site_chrome(page_path, "spreadsheets.html"),
        "<h1>Spreadsheets</h1>",
        f"<p>{len(with_summary)} threads contain spreadsheet data, of which "
        f"<strong>{embedded}</strong> are ordinary documents with a spreadsheet embedded. "
        "Quip's HTML flattens those to a plain table: cell types, number formats, column "
        "widths and the separation into sheets are all lost, and a document with three "
        "embedded sheets becomes three anonymous tables.</p>",
        f"<p><strong>{exported} of {len(with_summary)} have a real workbook exported "
        "alongside them</strong> (<code>/1/threads/{id}/export/xlsx</code>), linked in "
        "the XLSX column and from the document page itself. The workbook keeps cell "
        "types, styles, column widths and the sheets as separate sheets. Two things it "
        "does <em>not</em> keep: <strong>formulas</strong> arrive as their computed "
        "values — Quip's own exporter writes no formula elements, despite the API "
        "reporting <code>has_formulas</code> — and <strong>charts</strong> are absent "
        "from both formats.</p>",
        _table(["Document", "Folder", "Kind", "Tables", "Rows", "Cols", "Lost",
                "XLSX", "Source"], rows),
    ]
    if heavy_rows:
        body += [
            "<h2>Table-heavy documents</h2>",
            "<p>These carry no spreadsheet metadata but contain several tables, so they "
            "may still deserve a look.</p>",
            _table(["Document", "Folder", "Tables", "Source"], heavy_rows),
        ]
    return page("Spreadsheets needing attention", "\n".join(body), STYLESHEET_NAME,
                body_class="quip-report-page", extra_head=chrome_scripts(page_path), footer=footer())


def slide_decks(cfg, threads) -> str:
    page_path = "slide-decks.html"
    decks = sorted((t for t in threads.values() if t.is_slides),
                   key=lambda t: t.display_title.lower())
    rows = [[
        _title_cell(page_path, t),
        H.escape_text(_folder_of(t)),
        H.escape_text(format_date(t.updated_usec)),
        _quip_link(t.link),
    ] for t in decks]
    body = [
        icon_sprite(), site_chrome(page_path, "slide-decks.html"),
        "<h1>Slide decks</h1>",
        f"<p>{len(decks)} slide decks. Quip returns HTML for slides, and it is richer "
        "than expected, but it is laid out for a slide canvas rather than a page — so "
        "these render as a long sequence of tables and need a dedicated pass. This is a "
        "known limitation.</p>",
        _table(["Deck", "Folder", "Last modified", "Source"], rows),
    ]
    return page("Slide decks", "\n".join(body), STYLESHEET_NAME,
                body_class="quip-report-page", extra_head=chrome_scripts(page_path), footer=footer())


def import_notes(cfg, threads, reports, discovery: dict) -> str:
    """Everything the archive could not carry across, in one place.

    The per-document problems come first because they are the ones a reader can
    act on; the format-wide notes (formulas, slides, live apps) follow.
    """
    page_path = IMPORT_NOTES
    sections = []

    rows = []
    for t, rep in reports:
        problems = []
        # Split by what was actually lost -- "3 missing images" is actionable in
        # a way that "3 missing image/file" is not.
        missing = Counter(e.get("blob_kind") or "file"
                          for e in rep.entries if e["kind"] == "missing_blob")
        for blob_kind, n in sorted(missing.items()):
            problems.append(f'<span class="quip-flag">{n} missing {blob_kind}'
                            f'{"s" if n != 1 else ""}</span>')
        n_comment_files = rep.count("missing_comment_file")
        if n_comment_files:
            problems.append(f'<span class="quip-flag">{n_comment_files} missing file'
                            f'{"s" if n_comment_files != 1 else ""} in comments</span>')
        n_live = rep.count("live_app_placeholder")
        if n_live:
            problems.append(f'<span class="quip-flag">{n_live} live app not rendered</span>')
        n_attach = sum(e.get("count", 0) for e in rep.entries
                       if e["kind"] == "unexported_attachment")
        if n_attach:
            problems.append(f'<span class="quip-flag">{n_attach} missing Quip '
                            f'file/date mention{"s" if n_attach != 1 else ""}</span>')
        # The most serious kind of loss: a document that reads as complete and
        # is a prefix. Listed first because it is the one worth acting on.
        if rep.has("incomplete_html"):
            e = next(x for x in rep.entries if x["kind"] == "incomplete_html")
            problems.insert(0, '<span class="quip-flag">TRUNCATED &mdash; only '
                            f'{e.get("pages", 0)} page(s) of content</span>')
        if rep.has("no_content"):
            problems.append('<span class="quip-flag">no content returned</span>')
        if rep.has("parse_warning"):
            problems.append('<span class="quip-flag">parse warning</span>')
        # Unanchored comments are not listed: every comment is still on its
        # document, under "Unanchored comments". Only its position is unknown,
        # because the passage it was written about was later edited in Quip.
        if t.has_charts:
            problems.append('<span class="quip-flag">charts lost</span>')
        if not problems:
            continue
        rows.append([
            _title_cell(page_path, t),
            H.escape_text(_folder_of(t)),
            " ".join(problems),
            _quip_link(t.link),
        ])
    sections.append("<h2>Documents with content loss</h2>")
    sections.append(
        _table(["Document", "Folder", "Issues", "Source"], rows) if rows
        else "<p>None. Every document rendered without loss.</p>")
    sections.append("<h3>About missing images</h3>")
    sections.append(
        "<p>Blob permissions in Quip are independent of document permissions, so a "
        "document you can read may contain images you cannot. Those appear inline as "
        "<span class=\"quip-missing-image\">[missing: …]</span> and are reported under "
        "&ldquo;content loss&rdquo;. The same goes for files attached to comments and "
        "chat messages, which appear as "
        "<span class=\"quip-missing-image\">[missing file: …]</span>.</p>")

    attach_total = sum(e.get("count", 0) for _t, rep in reports
                       for e in rep.entries if e["kind"] == "unexported_attachment")
    attach_docs = sum(1 for _t, rep in reports if rep.has("unexported_attachment"))
    sections.append("<h3>About missing Quip file/date mentions</h3>")
    if attach_total:
        sections.append(
            f"<p><strong>{attach_total} file/date mentions across {attach_docs} "
            "documents came back empty from Quip's API.</strong> Most are attached "
            "files: Quip's API "
            "represents one as an empty <code>&lt;control&gt;</code> element carrying no "
            "filename, no identifier and no link, and the file is absent from the V1 "
            "HTML, the V2 HTML, the DOCX export and the messages API. But the API "
            "returns date mentions the same way, and nothing in its response says "
            "which one a given empty mention was. Each is marked in place as "
            "<span class=\"quip-missing-attachment\">[missing Quip file/date "
            "mention]</span>. Opening "
            "the original in Quip shows what it was; a file has to be downloaded from "
            "there by hand.</p>")
    else:
        sections.append("<p>No missing Quip file/date mentions were detected.</p>")

    restricted = discovery.get("restricted_folders") or []
    sections.append("<h2>Inaccessible folders</h2>")
    if restricted:
        sections.append(
            f"<p>{len(restricted)} folders returned a permission error and could not be "
            "walked. Their contents are absent from this archive.</p>"
            + _table(["Folder ID"], [[_icon_label("folder", f)] for f in restricted]))
    else:
        sections.append("<p>None.</p>")

    sections.append(_formulas_note(threads))
    sections.append(_slides_note(threads))
    sections.append(_live_apps_section(page_path, reports))

    body = (icon_sprite() + site_chrome(page_path, IMPORT_NOTES)
            + "<h1>Import notes</h1>"
            + "\n".join(sections))
    return page("Import notes", body, STYLESHEET_NAME,
                body_class="quip-report-page", extra_head=chrome_scripts(page_path), footer=footer())


def _formulas_note(threads) -> str:
    """Formulas are lost in both formats, and neither page says so on its own.

    Called out here rather than only on `spreadsheets.html` because this is the
    page a human reads to find out what the export could not carry, and a
    spreadsheet that silently became a grid of frozen numbers is exactly that.
    """
    with_formulas = [t for t in threads.values() if t.has_formulas]
    with_charts = [t for t in threads.values() if t.has_charts]
    exported = sum(1 for t in threads.values() if t.xlsx_path)
    if not with_formulas and not with_charts:
        return ("<h2>About spreadsheet formulas</h2>"
                "<p>No spreadsheet in this export reports formulas or charts.</p>")

    bits = ["<h2>About spreadsheet formulas</h2>"]
    if with_formulas:
        bits.append(
            f"<p><strong>{len(with_formulas)} threads contain formulas that did not "
            "survive.</strong> Every spreadsheet-bearing thread has a real workbook "
            f"exported beside it ({exported} in total), which keeps cell types, styles, "
            "column widths and separate sheets &mdash; but <em>not</em> the formulas. "
            "Quip's own <code>/1/threads/{id}/export/xlsx</code> writes no formula "
            "elements at all, even for the threads whose metadata reports "
            "<code>has_formulas</code>. So both the page and the workbook show computed "
            "values: the numbers are right, but the logic that produced them is gone, "
            "and editing a cell will not recalculate anything.</p>")
    if with_charts:
        bits.append(
            f"<p><strong>{len(with_charts)} threads contain charts</strong>, which are "
            "absent from the HTML and the workbook alike. The underlying data is still "
            "there; the chart is not.</p>")
    bits.append('<p><a href="spreadsheets.html">Spreadsheets</a> lists every affected '
                "document with its sheet count, size and workbook download.</p>")
    return "\n".join(bits)


def _slides_note(threads) -> str:
    decks = [t for t in threads.values() if t.is_slides]
    if not decks:
        return ("<h2>About slide decks</h2>"
                "<p>This export contains no slide decks.</p>")
    return (
        "<h2>About slide decks</h2>"
        f"<p><strong>{len(decks)} slide decks came across as raw Quip HTML.</strong> "
        "Quip returns markup for slides and the text is all present, but it is laid out "
        "for a slide canvas &mdash; absolutely positioned layers inside a grid &mdash; "
        "so on a page it reads as a long sequence of tables rather than as slides. "
        "Nothing is missing; it is the arrangement that does not survive. A dedicated "
        "renderer, or Quip's asynchronous PDF export, would read far better.</p>"
        '<p><a href="slide-decks.html">Slide decks</a> lists them.</p>')


def _live_apps_section(page_path: str, reports) -> str:
    """Every document containing a live app.

    A live app is a dynamic widget frozen into static HTML, so all of them are
    worth a human glance even when nothing was lost. Ordered by how much
    attention each one actually needs: apps that render nothing first, then
    those whose rendering dropped rows we had to recover from the payload, then
    the ones that came across intact.
    """
    rows = []
    for t, rep in reports:
        entry = next((e for e in rep.entries if e["kind"] == "live_apps"), None)
        if entry is None:
            continue
        empty, recovered = entry.get("empty", 0), entry.get("recovered", 0)
        if empty:
            status = (f'<span class="quip-flag">{empty} showing nothing</span>')
        elif recovered:
            status = (f'<span class="quip-flag">{recovered} recovered from payload</span>')
        else:
            status = "shown as Quip rendered them"
        rows.append((empty, recovered, t.display_title.lower(), [
            _title_cell(page_path, t),
            _folder_cell(page_path, t),
            str(entry["count"]),
            H.escape_text(", ".join(entry.get("names") or [])),
            status,
            _quip_link(t.link),
        ]))
    if not rows:
        return ("<h2>Live apps are static snapshots</h2>"
                "<p>None. This account contains no live apps.</p>")

    rows.sort(key=lambda r: (-r[0], -r[1], r[2]))
    total = sum(int(r[3][2]) for r in rows)
    empty_docs = sum(1 for r in rows if r[0])
    recovered_docs = sum(1 for r in rows if r[1] and not r[0])

    lead = (f"<p><strong>{total} live apps across {len(rows)} documents.</strong> "
            "Each is shown using Quip's own rendering of it, expanded in place — a "
            "static snapshot of something that was interactive, so the data survives "
            "but the behaviour does not.</p>")
    if empty_docs:
        lead += (f"<p><strong>{empty_docs} documents contain an app that renders "
                 "nothing at all</strong> and need a human look; they are listed first "
                 "and also appear under content loss above.</p>")
    if recovered_docs:
        lead += (f"<p>In {recovered_docs} documents Quip's own rendering dropped rows "
                 "— cards, or a whole column — which were recovered by decoding the "
                 "app's stored payload and are shown above the rendering. Worth "
                 "checking against the original.</p>")
    if not empty_docs and not recovered_docs:
        lead += "<p>Every one of them came across with its content intact.</p>"
    lead += ("<p>Payload decoding covers Kanban, Calendar and Project Tracker; other "
             "types show only Quip's rendering.</p>")

    return ("<h2>Live apps are static snapshots</h2>"
            "<p>Quip's export includes some live app data, but you may want to "
            "spot-check important documents.</p>"
            + lead
            + "<h3>Documents with live apps</h3>"
            + _table(["Document", "Folder", "Apps", "Types", "Status", "Source"],
                     [r[3] for r in rows]))


def _share_bucket(thread) -> str:
    """The sharing filter's three buckets, from the same source as the label."""
    if (thread.sharing or {}).get("company_mode"):
        return "company"
    ids = {u for u in (thread.member_ids or thread.expanded_user_ids or []) if u}
    return "private" if len(ids) <= 1 else "shared"


def search_index(threads, folders) -> str:
    """`quip-search-index.js`: one shared copy of what every page searches.

    Written as a script that assigns a global rather than as JSON, because
    Chrome blocks fetch/XHR of a sibling file on `file://` URLs -- the archive
    has to work with no server. One file rather than an inlined copy per page:
    the payload is around a megabyte, and there are as many pages as documents.

    Keys are short for the same reason: `t p f k c u cr m s`.
    """
    entries = []
    for t in threads.values():
        ids = {u for u in (t.member_ids or t.expanded_user_ids or []) if u}
        entries.append({"t": t.display_title, "p": t.output_path,
                        "f": _folder_of(t), "k": t.type,
                        "u": t.updated_usec or 0, "cr": t.created_usec or 0,
                        "m": len(ids), "s": _share_bucket(t)})
    for f in folders:
        if f.output_path:
            entries.append({"t": f.display_title, "p": f"{f.output_path}/index.html",
                            "f": f.output_path, "k": "folder", "c": f.color,
                            "u": f.updated_usec or 0, "cr": 0,
                            "m": len(f.member_ids or []), "s": ""})
    entries.sort(key=lambda e: e["t"].lower())
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return f"window.QUIP_INDEX = {payload};\n"


def index(cfg, threads, folders, discovery: dict) -> str:
    """The local browser view: folder tree, plus the shared nav and search.

    The search index used to be built and inlined here. It now lives in
    `quip-search-index.js` (see `search_index`), because every page carries the
    same search box and 1,592 inlined copies of a megabyte is not a thing to
    put on disk.
    """
    roots = [f for f in folders if not f.parent_id]
    tree_html = "\n".join(_tree_node(f, folders, threads) for f in roots)
    tree_html += _unfiled_node(folders, threads)

    # The search index is no longer inlined here: it lives in
    # `quip-search-index.js` so that every page can search, not just this one.
    script = chrome_scripts("index.html")

    counts = {}
    for t in threads.values():
        counts[t.type] = counts.get(t.type, 0) + 1
    summary = ", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(counts.items()))

    body = f"""{icon_sprite()}
{site_chrome("index.html", "index.html")}
<h1>Imported from Quip</h1>
<p>{H.escape_text(discovery.get('user_name') or '')} &middot;
{H.escape_text(discovery.get('site') or '')} &middot; {H.escape_text(summary)}
across {len(folders)} folders.</p>
<div id="tree"><h2>Folders</h2>
<ul class="quip-tree">{tree_html}</ul></div>"""

    return page("Imported from Quip", body, STYLESHEET_NAME,
                body_class="quip-index", extra_head=script, footer=footer())


def entry_row(title: str, href: str, kind: str, updated_usec=0,
              extra_class: str = "", color: str = "") -> str:
    """Icon, title, relative date -- the shared row shape.

    Mirrors `QuipUtils.entryHtml` in quip-utils.js so search results and the
    static tree render identically. The relative date is filled in by that
    script, since "3 months ago" is only true on the day it is written.
    """
    when = (f'<span class="quip-when" data-quip-updated="{int(updated_usec)}"></span>'
            if updated_usec else "")
    # Folder rows carry their own class wherever they appear -- the tree, folder
    # listings, search results -- so the stylesheet can weight them in one place.
    classes = ["quip-entry"]
    if kind == "folder":
        classes.append("quip-folder")
    classes += [c for c in extra_class.split() if c not in classes]
    cls = " ".join(classes)
    return (f'<a class="{cls}" href="{H.escape_attr(href)}">'
            f"{icon_use(kind, color)}"
            f'<span class="quip-entry-title">{H.escape_text(title)}</span>'
            f"{when}</a>")


# A leaf keeps the same left gutter as a branch so every icon lines up.
LEAF_GUTTER = '<span class="quip-toggle-spacer" aria-hidden="true"></span>'


def _toggle_button() -> str:
    """Collapses its folder. A button, not a link, so it never navigates.

    The arrow is Quip's own triangle from the sprite, rotated by CSS rather than
    swapped for a second icon, so the two states cannot drift apart.
    """
    return ('<button type="button" class="quip-toggle" aria-expanded="true" '
            'aria-label="Collapse folder">'
            '<svg class="quip-toggle-icon" aria-hidden="true" focusable="false">'
            '<use href="#quip-icon-triangle-right"></use></svg>'
            "</button>")


def _unfiled_node(folders, threads) -> str:
    """Documents no folder claims, so the tree can never hide one.

    The tree is built by walking folders, which means a thread belonging to no
    folder -- because its folder is restricted, or because it arrived through
    `refresh` before any folder listed it -- would otherwise be reachable only
    through the search box. `verify`'s index_complete check fails if this is
    ever wrong.
    """
    filed = {tid for f in folders for tid in f.child_thread_ids}
    unfiled = sorted((t for t in threads.values() if t.id not in filed),
                     key=lambda t: t.display_title.lower())
    if not unfiled:
        return ""
    rows = "".join(
        '<li><div class="quip-row">' + LEAF_GUTTER
        + entry_row(t.display_title, t.output_path, t.type, t.updated_usec)
        + "</div></li>" for t in unfiled)
    return ('<li><div class="quip-row">' + _toggle_button()
            + '<span class="quip-entry quip-folder"><span class="quip-entry-title">'
            + f"Not in any folder ({len(unfiled)})</span></span></div>"
            + f'<ul class="quip-tree">{rows}</ul></li>')


def _tree_node(folder, folders, threads, depth=0) -> str:
    if depth > 12:
        return ""
    children = [f for f in folders if f.parent_id == folder.id]
    docs = [threads[t] for t in folder.child_thread_ids if t in threads]

    inner, toggle = "", LEAF_GUTTER
    if children or docs:
        parts = [_tree_node(c, folders, threads, depth + 1) for c in
                 sorted(children, key=lambda f: f.display_title.lower())]
        for t in sorted(docs, key=lambda t: t.display_title.lower()):
            parts.append(
                '<li><div class="quip-row">' + LEAF_GUTTER
                + entry_row(t.display_title, t.output_path, t.type, t.updated_usec)
                + "</div></li>")
        inner = f'<ul class="quip-tree">{"".join(parts)}</ul>'
        toggle = _toggle_button()

    row = ('<div class="quip-row">' + toggle
           + entry_row(folder.display_title, f"{folder.output_path}/index.html",
                       "folder", folder.updated_usec, extra_class="quip-folder",
                       color=folder.color)
           + "</div>")
    return "<li>" + row + inner + "</li>"


# -- archive -------------------------------------------------------------

def make_zip(cfg, include_raw: bool = False) -> Path:
    """Zip the human-readable export. Raw cache excluded unless asked for."""
    out = cfg.base_dir / f"export-{cfg.export_id}.zip"
    skip_roots = {"raw", "state"} if not include_raw else set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(cfg.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(cfg.root)
            if rel.parts and rel.parts[0] in skip_roots:
                continue
            z.write(path, Path(f"export-{cfg.export_id}") / rel)
    log.info("Wrote %s (%.1f MB)", out, out.stat().st_size / 1e6)
    return out


def write_all(cfg, result, discovery: dict) -> dict:
    threads, reports = result["threads"], result["reports"]
    folders = result["folders"]

    # An unmapped type would silently render as a document icon, which is wrong
    # rather than merely plain. Say so instead of absorbing it.
    unmapped = unknown_icon_kinds(threads)
    if unmapped:
        log.warning("No icon for thread type(s) %s; they render as documents. "
                    "Add them to ICON_BY_KIND.", sorted(unmapped))
        (cfg.extras_dir / "anomalies" / "unmapped-icon-types.json").write_text(
            json.dumps(sorted(unmapped), indent=1), encoding="utf-8")

    pages = {
        "recent.html": recent(cfg, threads),
        "spreadsheets.html": spreadsheets(cfg, threads),
        "slide-decks.html": slide_decks(cfg, threads),
        IMPORT_NOTES: import_notes(cfg, threads, reports, discovery),
        "index.html": index(cfg, threads, folders, discovery),
    }
    for name, html in pages.items():
        (cfg.root / name).write_text(html, encoding="utf-8")
    # Both were merged into import-notes.html. In an older export, document
    # pages not yet re-rendered still link to them from their nav bar, so each
    # becomes a forward rather than a dead link or a stale copy.
    forward = (f'<!doctype html><meta charset="utf-8"><title>Import notes</title>'
               f'<meta http-equiv="refresh" content="0; url={IMPORT_NOTES}">'
               f'<p>Moved to <a href="{IMPORT_NOTES}">Import notes</a>.</p>\n')
    for old in ("needs-attention.html", "limitations.html"):
        if (cfg.root / old).exists():
            (cfg.root / old).write_text(forward, encoding="utf-8")

    # Shared by every page's search box. One copy, not one per document.
    (cfg.root / SEARCH_INDEX_NAME).write_text(
        search_index(threads, folders), encoding="utf-8")

    # Machine-readable companion for anything downstream.
    anomalies = [{"thread_id": t.id, "title": t.display_title,
                  "path": t.output_path, "entries": rep.entries}
                 for t, rep in reports if rep.entries]
    (cfg.extras_dir / "anomalies" / "render-report.json").write_text(
        json.dumps(anomalies, indent=1), encoding="utf-8")

    log.info("Wrote %d report pages", len(pages))
    return {"pages": list(pages)}
