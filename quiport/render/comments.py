"""Comments and document chat.

Both come from `/1/messages/{thread_id}`; a message with an `annotation` is an
inline comment, one without is document-level chat.

Two measured realities shape this module:

  * The field is `highlight_section_ids`, **not** the documented
    `highlighted_section_ids`.
  * Most anchors are stale. In the test account only 2 of 13 resolved to a
    section still present in the document -- the commented-on text was edited
    away years ago. Dropping unresolvable anchors would discard ~85% of the
    comments, so they are kept under an "Unanchored comments" heading instead.
"""

from __future__ import annotations

import logging
import re

from .. import htmlutil as H
from ..model import Message, format_datetime, format_exact

log = logging.getLogger(__name__)

QUIP_LINK_RE = re.compile(r"https?://[\w.-]*quip\.com/([\w-]+)")


class CommentSet:
    """Comments grouped into numbered footnotes, plus the chat log."""

    def __init__(self):
        self.anchored: list[dict] = []     # {number, section_id, messages}
        self.unanchored: list[dict] = []   # {number, messages}
        self.chat: list[Message] = []

    @property
    def total_groups(self) -> int:
        return len(self.anchored) + len(self.unanchored)


def build(messages_raw: list, tree, report) -> CommentSet:
    """Group messages by annotation and decide which anchors are usable."""
    out = CommentSet()
    messages = [Message.from_raw(m) for m in (messages_raw or [])]
    messages = [m for m in messages if m.visible]        # deleted are out of scope

    groups: dict[str, list[Message]] = {}
    order: list[str] = []
    for m in messages:
        if m.is_comment:
            if m.annotation_id not in groups:
                groups[m.annotation_id] = []
                order.append(m.annotation_id)
            groups[m.annotation_id].append(m)
        else:
            out.chat.append(m)

    # Resolve anchors against the document, then number anchored groups in
    # document order so footnote 1 is the first marker a reader meets.
    resolved: list[tuple[int, str, list[Message]]] = []
    unresolved: list[list[Message]] = []
    section_positions = _section_positions(tree)

    for ann_id in order:
        msgs = sorted(groups[ann_id], key=lambda m: m.created_usec)
        target = None
        for m in msgs:
            for sid in m.highlight_section_ids:
                if sid in section_positions:
                    target = sid
                    break
            if target:
                break
        if target:
            resolved.append((section_positions[target], target, msgs))
        else:
            unresolved.append(msgs)

    resolved.sort(key=lambda t: t[0])
    number = 1
    for _pos, sid, msgs in resolved:
        out.anchored.append({"number": number, "section_id": sid,
                             # Captured now, while the tree is in hand: the
                             # comment section is assembled long after this.
                             "excerpt": _excerpt(tree, sid),
                             "messages": msgs})
        number += 1
    for msgs in unresolved:
        out.unanchored.append({"number": number, "messages": msgs})
        number += 1

    if unresolved:
        report.add("unanchored_comments", count=len(unresolved),
                   anchored=len(resolved))
    return out


# Roughly two lines at the comment column's width. Long enough to recognise the
# passage, short enough not to become a second copy of the document.
EXCERPT_CHARS = 160


def _excerpt(tree, section_id: str) -> str:
    """The opening of the text a comment was written about.

    Replaces the bare footnote number: "1" tells a reader nothing once they have
    scrolled away from the marker, whereas the first line of the passage says
    immediately what the conversation is about.

    Trimmed on a word boundary, and markers this exporter itself inserted are
    excluded so the quote is the author's text rather than our annotations.
    """
    el = H.find_by_id(tree, section_id)
    if el is None:
        return ""
    parts = []
    for child in el.children:
        if isinstance(child, H.Element) and any(
                c.startswith("quip-") for c in child.classes()):
            continue                      # our own marker, not the document
        parts.append(child.text())
    text = re.sub(r"\s+", " ", "".join(parts)).strip().strip("​").strip()
    if len(text) <= EXCERPT_CHARS:
        return text
    cut = text[:EXCERPT_CHARS]
    if " " in cut[EXCERPT_CHARS // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.") + "…"


def _section_positions(tree) -> dict:
    """id -> document order index, for every element carrying an id."""
    positions = {}
    for i, el in enumerate(H.iter_elements(tree)):
        el_id = el.get("id")
        if el_id and el_id not in positions:
            positions[el_id] = i
    return positions


def insert_markers(tree, comments: CommentSet) -> int:
    """Put a comment bubble in the left gutter of each anchored line.

    Quip marks a commented line with a bubble in the margin carrying the number
    of comments on it, so the archive does the same rather than using a
    superscript footnote number. Clicking it jumps to the full conversation at
    the bottom of the page.

    The bubble is prepended rather than appended: CSS lifts it into the gutter,
    but on a narrow screen there is no gutter to lift it into, and it then falls
    back to sitting inline at the start of the line, where it still reads
    correctly.
    """
    placed = 0
    for group in comments.anchored:
        el = H.find_by_id(tree, group["section_id"])
        if el is None:
            continue
        count = len(group["messages"])
        label = f"{count} comment" + ("" if count == 1 else "s")

        marker = H.Element("a", {
            # quip-comment-ref is retained: `verify` treats it as generated
            # markup and strips it before comparing page text against raw.
            "class": "quip-comment-ref quip-comment-gutter",
            "href": f"#quip-comment-{group['number']}",
            "id": f"quip-comment-ref-{group['number']}",
            "title": label,
            "aria-label": label,
        })
        marker.append(_bubble(count))

        el.children.insert(0, marker)
        marker.parent = el
        el.add_class("quip-has-comment")
        placed += 1
    return placed


def _bubble(count: int):
    """Quip's comment bubble from the sprite, with the count drawn inside."""
    svg = H.Element("svg", {"class": "quip-comment-bubble", "viewBox": "0 0 20 20",
                            "aria-hidden": "true", "focusable": "false"})
    svg.append(H.Element("use", {"href": "#quip-icon-comment"}))
    label = str(count) if count < 100 else "99+"
    text = H.Element("text", {
        "x": "10", "y": "13", "text-anchor": "middle",
        # The bubble's opening is small; more digits have to be set smaller.
        "class": f"quip-count-{min(len(label), 3)}",
    })
    text.append(H.Text(label))
    svg.append(text)
    return svg


def render_comments(comments: CommentSet, blob_resolver, page_path: str,
                    name_resolver=None) -> str:
    """The footnote section at the bottom of the document."""
    if not comments.anchored and not comments.unanchored:
        return ""

    parts = ['<hr class="quip-rule">',
             '<section class="quip-comments">',
             "<h2>Imported Quip comments</h2>"]

    for group in comments.anchored:
        parts.append(_group_html(group, page_path, blob_resolver, True, name_resolver))

    if comments.unanchored:
        parts.append('<h3 class="quip-unanchored-heading">Unanchored comments</h3>')
        parts.append('<p class="quip-comment-context">The position of these comments is '
                     'unknown, they refer to text that has been edited or removed.</p>')
        for group in comments.unanchored:
            parts.append(_group_html(group, page_path, blob_resolver, False, name_resolver))

    parts.append("</section>")
    return "\n".join(parts)


def _group_html(group, page_path, blob_resolver, anchored: bool, name_resolver=None) -> str:
    """One conversation: what it is about, then who said what.

    The heading is the quoted passage rather than a footnote number, and it is
    the link back to the text -- so the reader can see what is being discussed
    without following anything.
    """
    n = group["number"]
    lines = [f'<div class="quip-comment-group" id="quip-comment-{n}">']

    excerpt = group.get("excerpt") or ""
    if anchored and excerpt:
        lines.append(
            f'<a class="quip-comment-quote" href="#quip-comment-ref-{n}" '
            f'title="Go to this passage in the document">'
            f"{H.escape_text(excerpt)}</a>")
    elif anchored:
        # Anchored, but the passage has no text of its own -- an image or a
        # table cell. The link back still has to exist.
        lines.append(
            f'<a class="quip-comment-quote quip-comment-quote-empty" '
            f'href="#quip-comment-ref-{n}" '
            f'title="Go to this passage in the document">'
            f"Comment {n} in the document</a>")

    lines.append('<div class="quip-comment-list">')
    for m in group["messages"]:
        lines.append(_message_html(m, page_path, blob_resolver, name_resolver))
    lines += ["</div>", "</div>"]
    return "\n".join(lines)


def render_chat(comments: CommentSet, blob_resolver, page_path: str,
                heading="Imported document chat messages", name_resolver=None) -> str:
    """The transcript, with a dated rule where the day changes.

    Quip separates days that way, and in a log spanning years it is the only
    thing that makes the shape of the conversation legible.
    """
    if not comments.chat:
        return ""
    lines = ['<hr class="quip-rule">',
             '<section class="quip-chat-log">',
             f"<h2>{H.escape_text(heading)}</h2>",
             '<div class="quip-chat-list">']
    previous_day = None
    for m in comments.chat:
        day = format_datetime(m.created_usec)
        if day != previous_day and day != "unknown":
            lines.append(f'<div class="quip-day-divider"><span>'
                         f"{H.escape_text(day)}</span></div>")
            previous_day = day
        lines.append(_message_html(m, page_path, blob_resolver, name_resolver))
    lines += ["</div>", "</section>"]
    return "\n".join(lines)


def _message_html(m: Message, page_path: str, blob_resolver, name_resolver=None) -> str:
    """`Alex (Mar 9, 2021): Hi!` plus any attached images.

    Quip stores an @mention of a person as a bare `https://quip.com/<user_id>`
    URL in the message text and renders it as the person's name. Left alone it
    exports as a raw link to nothing useful, so mentions are resolved back to
    names here.
    """
    author = H.escape_text(m.author_name or "Unknown")
    when = H.escape_text(format_datetime(m.created_usec))
    text = H.escape_text(m.text or "")

    mentioned = set(m.mention_user_ids or [])

    def _link_or_name(mo):
        matched = mo.group(1).rstrip(".,;:!?)")
        trailing = mo.group(1)[len(matched):]
        # `text` was HTML-escaped above, so `matched` is escaped too. The href
        # needs the URL escaped exactly *once*: escaping the escaped form turns
        # `?a=1&b=2` into `?a=1&amp;amp;b=2`, and the browser then requests a
        # URL containing a literal "&amp;". Invisible in the page, because the
        # anchor text still reads correctly -- only following the link fails.
        url = H.unescape_text(matched)
        token_match = QUIP_LINK_RE.match(url)
        token = token_match.group(1) if token_match else None
        if token:
            # `mention_user_ids` is authoritative; the name index catches
            # mentions of people not listed there.
            name = None
            if token in mentioned:
                name = (name_resolver(token) if name_resolver else None) or "Unknown person"
            elif name_resolver:
                name = name_resolver(token)
            if name:
                return (f'<strong class="quip-mention-person">{H.escape_text(name)}</strong>'
                        + trailing)
        # href from the raw URL; anchor text stays escaped -- it is a text
        # position, and `matched` is already correct there.
        return f'<a href="{H.escape_attr(url)}">{matched}</a>{trailing}'

    # Remaining links stay clickable; the link pass rewrites Quip document
    # links to relative paths afterwards.
    text = re.sub(r"(https?://\S+)", _link_or_name, text)

    # Name and date on their own line, message beneath -- the shape Quip uses.
    # The exact timestamp goes in `title` so the precision is not lost to the
    # friendlier date.
    exact = format_exact(m.created_usec)
    head = (f'<div class="quip-message-head">'
            f'<span class="quip-author">{author}</span>'
            f'<span class="quip-timestamp"'
            + (f' title="{H.escape_attr(exact)}"' if exact else "")
            + f">{when}</span></div>")
    body = f'{head}<div class="quip-message-body">{text}'

    for f in m.files or []:
        blob_id, name = f.get("hash"), f.get("name") or ""
        if not blob_id:
            continue
        rel = blob_resolver(None, blob_id, None, page_path, name_hint=name)
        if not rel:
            body += f' <span class="quip-missing-image">[missing: {H.escape_text(name or blob_id)}]</span>'
        elif re.search(r"\.(png|jpe?g|gif|webp|bmp|svg)$", rel, re.I):
            body += f'<img class="quip-comment-image" src="{H.escape_attr(rel)}" ' \
                    f'alt="{H.escape_attr(name)}">'
        else:
            body += f' <a href="{H.escape_attr(rel)}">{H.escape_text(name or blob_id)}</a>'
    # Attachments belong inside the body, so they sit under the author line.
    return f'<div class="quip-message">{body}</div></div>'
