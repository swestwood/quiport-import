"""Content transformations applied to a thread's V2 HTML.

Everything here works on the parsed tree from `htmlutil`, never on strings, so
malformed input cannot silently corrupt the output.

Quip's section styles, observed in the wild and in quip-export's stylesheet:
    5 bulleted list   6 numbered list   7 checklist   8 table
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

from .. import htmlutil as H
from ..model import usec_to_datetime

log = logging.getLogger(__name__)

BLOB_SRC_RE = re.compile(
    r"^(?:https?://[^/]*quip\.com)?/blob/([\w-]+)/([\w-]+)", re.IGNORECASE)

SECTION_BULLET, SECTION_NUMBER, SECTION_CHECKLIST, SECTION_TABLE = "5", "6", "7", "8"


# -- images and attachments ---------------------------------------------

def rewrite_blobs(tree, blob_resolver, page_path: str, report) -> None:
    """Point <img>/<a> at downloaded files, or mark them missing.

    A 403 on a blob is expected: blob permissions are independent of thread
    permissions, so a perfectly readable document can have unreadable images.
    Those become a visible placeholder, never a broken <img>.
    """
    for el in list(H.iter_elements(tree)):
        for attr in ("src", "href"):
            value = el.get(attr)
            if not value:
                continue
            m = BLOB_SRC_RE.match(value)
            if not m:
                continue
            thread_id, blob_id = m.group(1), m.group(2)
            rel = blob_resolver(thread_id, blob_id, el, page_path)
            if rel:
                el.set(attr, rel)
                el.add_class("quip-blob")
            else:
                _mark_missing(el, blob_id, report)


def _mark_missing(el, blob_id: str, report) -> None:
    """Replace an unfetchable blob with a marker that says what it was.

    The source element settles the kind with no guessing: Quip embeds a picture
    as `<img src='/blob/…'>` and a file as `<a href='/blob/…'>`. Only the
    *name* is optional -- 16 of the 50 blobs in the test account carry no `alt`,
    which is why these used to read as a generic "[missing: attachment]" even
    though every one of them was an image.
    """
    name = (el.get("alt") or el.get("title") or "").strip()
    kind = "image" if el.tag == "img" else "file"
    label = f"missing {kind}" + (f": {name}" if name else "")

    placeholder = H.Element("span", {
        "class": "quip-missing-image",
        "data-blob-id": blob_id,
        "data-quip-missing-kind": kind,
    })
    placeholder.append(H.Text(f"[{H.escape_text(label)}]"))
    parent = el.parent
    if isinstance(parent, H.Element):
        parent.replace(el, placeholder)
    # NOT `kind=` -- RenderReport.add puts the entry type under "kind", so that
    # keyword would overwrite it and hide the entry from every report.
    report.add("missing_blob", blob_id=blob_id, label=name, blob_kind=kind)


# -- empty image placeholders -------------------------------------------

# A data: URI this short is a spacer, not a picture. Quip's is the 1x1
# transparent GIF at 74 characters; the bound leaves room for variants without
# ever catching a real inline image.
MAX_SPACER_URI = 256


def render_image_placeholders(tree, report) -> int:
    """Shrink Quip's empty image wells to a small labelled box.

    An image the author never filled in exports as a 1x1 transparent GIF data
    URI carrying the well's canvas size -- `width='800' height='600'` -- so a
    browser renders it as an enormous blank hole in the page.

    This is safely distinguishable from both other cases, and must be, because
    they want opposite treatment:

      * a **real image** always has a `/blob/` src, never a data: URI
      * an **unfetchable** image also has a `/blob/` src, so it still goes
        through `rewrite_blobs` -> `_mark_missing` and is reported for a human

    Measured across the test corpus: 50 images with blob srcs, 3 with data
    URIs -- all three the identical 74-character spacer inside `div.empty`,
    none carrying an `alt`. So this is a flag, not a guess.
    """
    replaced = 0
    for img in list(H.iter_elements(tree, "img")):
        src = img.get("src") or ""
        if not src.startswith("data:"):
            continue
        parent = img.parent
        in_empty_well = isinstance(parent, H.Element) and "empty" in parent.classes()
        if not in_empty_well and len(src) > MAX_SPACER_URI:
            # A large inline image is real content. Leave it alone and say so,
            # rather than quietly deciding it is a placeholder.
            report.add("inline_data_image", bytes=len(src))
            continue

        size = f"{img.get('width') or '?'}x{img.get('height') or '?'}"
        box = H.Element("div", {
            "class": "quip-image-placeholder",
            "title": f"Empty image placeholder in Quip ({size})",
            "data-quip-placeholder-size": size,
        })
        box.append(H.Text("+ Add an image"))
        if isinstance(parent, H.Element) and parent.replace(img, box):
            replaced += 1

    if replaced:
        # Recorded but deliberately absent from import notes: nothing was
        # lost, the well is empty in Quip too.
        report.add("image_placeholder", count=replaced)
    return replaced


# -- attachments the API does not expose --------------------------------

def render_controls(tree, report, anchor_usec: int = 0) -> tuple[int, int]:
    """Handle Quip's generic `<control>` widget wrapper.

    `<control>` is not one thing. Measured across the test corpus: 6 of 126 are
    **empty** and the other 120 **carry text**: date mentions, person mentions,
    template field placeholders, and "insert" prompts. An empty one is usually
    an inline file attachment, whose bytes the API never exposes, but not
    always: "Welcome to Quip" has a date ("Complete by <control/>") that the API
    returns just as empty. Nothing in the markup tells the two apart, so the
    marker says what is known -- something is missing -- not what it was.

    So only an empty control may be replaced with a missing file/date mention marker.
    Replacing the rest would destroy real content, which is exactly what an
    earlier version of this function did.

    Returns (attachments_marked, dates_given_a_year).
    """
    attachments = dates = 0
    for control in list(H.iter_elements(tree, "control")):
        text = control.text().strip()
        parent = control.parent

        if not text:
            placeholder = H.Element("span", {
                "class": "quip-missing-attachment",
                "data-quip-control-id": control.get("id") or "",
            })
            placeholder.append(
                H.Text("[missing Quip file/date mention]"))
            if isinstance(parent, H.Element) and parent.replace(control, placeholder):
                attachments += 1
            continue

        # Content-bearing control: keep the text, tag it, and where it is a
        # date that Quip rendered without a year, supply the year.
        control.add_class("quip-control")
        dated = _add_year_to_date(control, text, anchor_usec)
        if dated:
            dates += 1
            control.add_class("quip-date-mention")

    if attachments:
        report.add("unexported_attachment", count=attachments)
    return attachments, dates


# "Monday, February 14" -- weekday present, year absent.
WEEKDAY_DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2})$")

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _add_year_to_date(control, text: str, anchor_usec: int) -> bool:
    """Recover the year Quip omitted from an inline date mention.

    Quip renders a date mention without a year when it falls in the document's
    own year, so the year is genuinely absent from the API response. It is
    still recoverable: the weekday pins it down. Only one year near the
    document's date has (say) 14 February falling on a Monday, so searching
    outward from the document's timestamp gives an unambiguous answer or none.

    Marked `title="Year inferred from the weekday"` so the inference is visible
    rather than passed off as data Quip supplied.
    """
    if not anchor_usec:
        return False
    m = WEEKDAY_DATE_RE.match(text)
    if not m:
        return False
    weekday, month_name, day = m.group(1), m.group(2), int(m.group(3))
    month = _MONTHS.index(month_name) + 1

    anchor = usec_to_datetime(anchor_usec)
    if anchor is None:
        return False

    matches = []
    for delta in range(0, 4):                      # search outward from the doc's year
        for year in {anchor.year - delta, anchor.year + delta}:
            try:
                candidate = dt.date(year, month, day)
            except ValueError:
                continue                            # e.g. 29 February in a common year
            if _WEEKDAYS[candidate.weekday()] == weekday:
                matches.append(year)
        if matches:
            break                                   # nearest year wins

    if len(set(matches)) != 1:
        return False                                # ambiguous: leave it alone

    year = matches[0]
    for child in list(control.children):
        if isinstance(child, H.Text) and text in child.data:
            child.data = child.data.replace(text, f"{text}, {year}")
            control.set("title", "Year inferred from the weekday")
            control.set("data-quip-inferred-year", str(year))
            return True
    return False


# -- checklists ---------------------------------------------------------

def render_checklists(tree) -> int:
    """Turn Quip's CSS-driven checklists into real checkboxes.

    Quip marks completion with `class="checked"` on the <li> and renders the box
    with a :before rule. That does not survive without the stylesheet, so we
    emit an actual disabled checkbox reflecting the state.
    """
    count = 0
    for div in H.iter_elements(tree, "div", lambda e: e.get("data-section-style") == SECTION_CHECKLIST):
        div.add_class("quip-checklist")
        for li in H.iter_elements(div, "li"):
            if "parent" in li.classes():        # grouping row, not a task
                continue
            checked = "checked" in li.classes()
            box = H.Element("input", {"type": "checkbox", "disabled": None})
            if checked:
                box.set("checked", None)
                li.add_class("quip-checked")
            li.children.insert(0, box)
            box.parent = li
            li.add_class("quip-task")
            count += 1
    return count


def tag_lists(tree) -> None:
    for style, cls in ((SECTION_BULLET, "quip-list-bulleted"),
                       (SECTION_NUMBER, "quip-list-numbered")):
        for div in H.iter_elements(tree, "div", lambda e, s=style: e.get("data-section-style") == s):
            div.add_class(cls)


def tag_tables(tree) -> int:
    n = 0
    for div in H.iter_elements(tree, "div", lambda e: e.get("data-section-style") == SECTION_TABLE):
        div.add_class("quip-spreadsheet")
        n += 1
    for table in H.iter_elements(tree, "table"):
        table.add_class("quip-table")
    return n


# -- readable text on coloured cells ------------------------------------

# WCAG 2.1: contrast against white is 1.05/(L+0.05); against black it is
# (L+0.05)/0.05. The two cross at L = 0.17913, where both are 4.58:1 -- below
# that, white is the more readable choice. The comparison is strict, so a colour
# sitting exactly on the crossover keeps black, which is the preference when it
# makes no measurable difference.
LIGHT_TEXT_LUMINANCE = 0.17913

HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
BACKGROUND_RE = re.compile(r"(?:^|;)\s*background(?:-color)?\s*:\s*([^;]+)", re.I)
HAS_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:", re.I)


def relative_luminance(hex_color: str) -> float | None:
    """WCAG 2.1 relative luminance of `#rgb` or `#rrggbb`, or None."""
    m = HEX_COLOR_RE.fullmatch((hex_color or "").strip())
    if not m:
        return None
    h = m.group(0)[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def wants_light_text(style: str) -> bool:
    """Is this element's own fill too dark for the default dark text?

    Only its *own* inline style counts. An explicit `color:` means the author
    already chose, and is left alone.
    """
    if not style or HAS_COLOR_RE.search(style):
        return False
    background = BACKGROUND_RE.search(style)
    if not background:
        return False
    found = HEX_COLOR_RE.search(background.group(1))
    if not found:
        return False
    luminance = relative_luminance(found.group(0))
    return luminance is not None and luminance < LIGHT_TEXT_LUMINANCE


def apply_readable_cell_text(tree) -> int:
    """Mark table cells whose fill is too dark to read black text on.

    Quip's own editor does this: past a certain darkness the default text colour
    flips to white. The HTML export carries the fill as an inline style but not
    that decision, so a dark navy cell exports as near-black text on near-black
    ground -- legible in Quip, unreadable in the archive.

    The choice has to be made here rather than in CSS, which cannot compute
    luminance. Cells that set their own `color` are untouched.
    """
    marked = 0
    for tag in ("td", "th"):
        for cell in H.iter_elements(tree, tag):
            if wants_light_text(cell.get("style") or ""):
                cell.add_class("quip-on-dark")
                marked += 1
    return marked


def tag_code_blocks(tree) -> int:
    n = 0
    for pre in H.iter_elements(tree, "pre"):
        pre.add_class("quip-code")
        n += 1
    return n


# -- live apps ----------------------------------------------------------

LIVE_APP_RENDERERS = {}


def render_live_apps(tree, name_by_section: dict, report) -> list:
    """Make live apps legible.

    Two sources describe the same app, and they are not equally good:

      * **Quip's own `<details>` dump** -- a server-rendered table with real
        headers, row colours as inline styles, and human text. Present on all
        110 live apps in the test corpus, never empty. This is the better
        rendering, so it is shown expanded rather than hidden behind a triangle.
      * **`data-live-app-payload`** -- structured JSON, on only 21 of the 110.
        Decoding it duplicates what the dump already shows.

    So the decoded table is emitted **only when it would show text the dump does
    not** -- measured at 1 of 110: a Kanban board whose dump rendered its column
    headers and dropped every card, while the payload still had them. Dropping
    the decoder entirely would have silently lost those.

    Only V1 fills in the app's name (V2 blanks the `<summary>`), so names arrive
    via `name_by_section` from the V1 HTML.
    """
    found = []
    for div in H.iter_elements(tree, "div", lambda e: e.has("data-live-app-id")):
        app_id = div.get("data-live-app-id") or ""
        section_id = div.get("id") or ""
        name = name_by_section.get(section_id) or _summary_text(div) or "Live app"
        payload_raw = div.get("data-live-app-payload")

        div.add_class("quip-liveapp")
        div.set("data-quip-live-app-name", name)

        dump_text = _match_key(" ".join(d.text() for d in H.iter_elements(div, "details")))
        has_dump = bool(dump_text)

        extra = False
        if payload_raw:
            try:
                node = _render_payload(name, json.loads(payload_raw))
                if node is not None and _adds_text(node, dump_text):
                    div.children.insert(0, node)
                    node.parent = div
                    extra = True
            except (ValueError, TypeError) as e:
                log.debug("live app payload unparsed (%s): %s", app_id, e)

        _expose_dump(div, name, labelled=not extra)

        found.append({"app_id": app_id, "name": name, "has_dump": has_dump,
                      "payload_added": extra, "has_payload": bool(payload_raw)})
        if extra:
            # Not a loss -- a recovery. Recorded so it is inspectable.
            report.add("live_app_payload_extra", name=name, app_id=app_id)
        if not has_dump and not extra:
            report.add("live_app_placeholder", name=name, app_id=app_id)
    return found


def _expose_dump(div, name: str, labelled: bool = True) -> None:
    """Show Quip's own rendering instead of hiding it behind a triangle.

    The `<details>` stays a `<details>` so a long dump can still be folded away,
    but it opens by default. V2 blanks the `<summary>`, which is why the
    triangle had no label; the app's name goes there.
    """
    details = next(iter(H.iter_elements(div, "details")), None)
    if details is None:
        marker = H.Element("p", {"class": "quip-liveapp-label"})
        marker.append(H.Text(f"[Live app: {H.escape_text(name)}]"))
        div.children.insert(0, marker)
        marker.parent = div
        return

    details.set("open", None)
    details.add_class("quip-liveapp-dump")

    summary = next(iter(H.iter_elements(details, "summary")), None)
    # When V1 gave us no name, `name` is already the generic "Live app"; saying
    # "Live app: Live app" is worse than saying nothing extra.
    text = name if name.strip().lower() == "live app" else f"Live app: {name}"
    label = H.Element("span", {"class": "quip-liveapp-label"})
    label.append(H.Text(H.escape_text(text)
                        + ("" if labelled else " (Quip's own rendering)")))
    if summary is None:
        summary = H.Element("summary")
        details.children.insert(0, summary)
        summary.parent = details
    if not summary.text().strip():
        summary.append(label)


def _match_key(text: str) -> str:
    """Normalize text for 'does the dump already say this?' comparisons.

    Case, whitespace and punctuation all differ between the two renderings --
    the dump writes `Name` where the payload writes `**Name**`, and splits a
    date range across two cells. Reducing both to bare alphanumerics compares
    what was said rather than how it was formatted.
    """
    return re.sub(r"[^0-9a-z]", "", (text or "").lower())


def _adds_text(node, dump_text: str) -> bool:
    """True if any cell of the decoded table is absent from the dump.

    Headers count only when they came from the payload -- a Kanban column is
    named by its data, but "Date" and "Event" over a calendar are labels this
    module invented, and no dump would ever contain them.
    """
    tags = ["td"]
    if node.get("data-quip-header-source") == "payload":
        tags.append("th")
    for tag in tags:
        for cell in H.iter_elements(node, tag):
            key = _match_key(cell.text())
            if key and key not in dump_text:
                return True
    return False


def _summary_text(div) -> str:
    for s in H.iter_elements(div, "summary"):
        t = s.text().strip()
        if t:
            return t
    return ""


def _render_payload(name: str, payload: dict):
    """Best-effort structured rendering for the app types Quip documents."""
    if not isinstance(payload, dict):
        return None
    if "columns" in payload and isinstance(payload.get("columns"), list):
        cols = payload["columns"]
        if cols and isinstance(cols[0], dict) and "cards" in cols[0]:
            return _render_kanban(cols)
        if "rows" in payload:
            return _render_tracker(cols, payload.get("rows") or [])
    if "events" in payload and isinstance(payload["events"], list):
        return _render_calendar(payload["events"])
    return None


def _table(class_name, headers, rows, header_source=""):
    """`header_source="payload"` marks headers that are data, not our labels."""
    attrs = {"class": class_name}
    if header_source:
        attrs["data-quip-header-source"] = header_source
    table = H.Element("table", attrs)
    if headers:
        thead = table.append(H.Element("thead"))
        tr = thead.append(H.Element("tr"))
        for h in headers:
            th = tr.append(H.Element("th"))
            th.append(H.Text(H.escape_text(str(h))))
    tbody = table.append(H.Element("tbody"))
    for row in rows:
        tr = tbody.append(H.Element("tr"))
        for cell in row:
            td = tr.append(H.Element("td"))
            td.append(H.Text(H.escape_text(str(cell if cell is not None else ""))))
    return table


def _render_kanban(columns):
    headers = [c.get("headerContent", "") for c in columns]
    depth = max((len(c.get("cards") or []) for c in columns), default=0)
    rows = []
    for i in range(depth):
        rows.append([
            (c.get("cards") or [{}] * depth)[i].get("content", "")
            if i < len(c.get("cards") or []) else ""
            for c in columns
        ])
    return _table("quip-liveapp-kanban", headers, rows, header_source="payload")


def _render_calendar(events):
    """No colour column: Quip's dump keeps the colour as an inline style and a
    `data-live-app-color` attribute on the row, which reads better than the bare
    word ORANGE in a table cell -- and a cell no dump could ever contain would
    make every calendar look like it had something extra to show."""
    rows = []
    for e in events:
        rng = e.get("dateRange") or {}
        start, end = rng.get("start", ""), rng.get("end", "")
        when = start if start == end or not end else f"{start} – {end}"
        rows.append([when, e.get("content", "")])
    return _table("quip-liveapp-calendar", ["Date", "Event"], rows)


def _render_tracker(columns, rows):
    headers = [str(c.get("content", "")).strip() for c in columns]
    ids = [str(c.get("id")) for c in columns]
    body = []
    for row in rows:
        body.append([_flatten(row.get(cid)) for cid in ids])
    return _table("quip-liveapp-tracker", headers, body, header_source="payload")


# Epoch milliseconds, bounded to 2001-2286 so ordinary counts are never caught.
EPOCH_MS_MIN, EPOCH_MS_MAX = 1_000_000_000_000, 10_000_000_000_000


def _flatten(value):
    """One payload cell as text.

    Date columns arrive as raw epoch milliseconds. Rendering `1596265200000`
    would be worse than useless here, because Quip's own dump drops these
    columns entirely -- the payload is the only surviving copy of the date.
    """
    if isinstance(value, list):
        return ", ".join(_flatten(v) for v in value)
    if value is None:
        return ""
    if isinstance(value, int) and not isinstance(value, bool) \
            and EPOCH_MS_MIN <= value < EPOCH_MS_MAX:
        stamp = usec_to_datetime(value * 1000)
        if stamp is not None:
            return stamp.strftime("%Y-%m-%d")
    return str(value).strip()


def live_app_names_from_v1(v1_html: str) -> dict:
    """Map section id -> live-app name using the V1 HTML.

    V2 blanks the app name in <summary>; V1 keeps it. This is the only reason
    the exporter still fetches V1 content, and it is never rendered directly.
    """
    if not v1_html:
        return {}
    names = {}
    try:
        tree = H.parse(v1_html)
    except Exception:
        return {}
    for div in H.iter_elements(tree, "div", lambda e: e.has("data-live-app-id")):
        sid = div.get("id")
        text = _summary_text(div)
        if sid and text:
            names[sid] = text
    return names


# -- structural notes ---------------------------------------------------

def count_tables(tree) -> int:
    return sum(1 for _ in H.iter_elements(tree, "table"))


def strip_empty_anchors(tree) -> None:
    """Quip emits zero-width spaces in empty paragraphs; keep them harmless."""
    for p in H.iter_elements(tree, "p"):
        if p.text().strip("​ \n\t") == "" and not p.children:
            p.add_class("quip-empty")
