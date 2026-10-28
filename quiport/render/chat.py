"""Render a standalone chat thread.

Chats have no document body -- only a message history -- but they live in
whatever Quip folder contains them, exactly like documents.
"""

from __future__ import annotations

from .. import htmlutil as H
from ..model import Thread, format_date
from ..naming import relative_path
from . import comments as C
from .document import RenderReport, _breadcrumbs, render_metadata
from .templates import STYLESHEET_NAME, footer, page


def render_chat(thread: Thread, ctx) -> tuple[str, RenderReport]:
    report = RenderReport(thread.id, thread.display_title)
    page_path = thread.output_path
    css = relative_path(page_path, STYLESHEET_NAME)

    empty_tree = H.parse("")
    comment_set = C.build(thread.messages, empty_tree, report)
    blob_resolver = ctx.blobs.resolver_for(thread)

    # Every message in a chat is the conversation itself, so anchoring is
    # meaningless here: render them as one transcript in time order.
    transcript = C.render_chat(comment_set, blob_resolver, page_path,
                               heading="Conversation", name_resolver=ctx.user_name)
    if not transcript and not comment_set.anchored and not comment_set.unanchored:
        transcript = '<p class="quip-empty-chat">No messages were returned for this chat.</p>'
        report.add("empty_chat")

    extra = C.render_comments(comment_set, blob_resolver, page_path, ctx.user_name)

    body = "\n".join(p for p in [
        _breadcrumbs(thread, ctx, page_path),
        f"<h1>{H.escape_text(thread.display_title)}</h1>",
        render_metadata(thread, ctx, page_path),
        transcript,
        extra,
    ] if p)

    report.add("stats", chat_messages=len(comment_set.chat),
               comments=comment_set.total_groups, tables=0,
               checkboxes=0, live_apps=0)

    return page(thread.display_title, body, css,
                body_class="quip-chat-page",
                footer=footer(ctx.exported_at(thread.id))), report
