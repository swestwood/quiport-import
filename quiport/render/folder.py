"""Render a folder as an index page with metadata and its contents."""

from __future__ import annotations

from .. import htmlutil as H
from ..model import Folder, format_date
from ..naming import path_to_root, relative_path
from .templates import (STYLESHEET_NAME, UTILS_NAME, breadcrumb, chrome_scripts,
                        footer, site_chrome,
                        icon_sprite, page)


def render_folder(folder: Folder, entries: list, ctx) -> str:
    """`entries`: [{title, path, kind, updated_usec}] already sorted."""
    page_path = f"{folder.output_path}/index.html"
    css = relative_path(page_path, STYLESHEET_NAME)

    crumbs = _breadcrumbs(folder, ctx, page_path)
    meta = _metadata(folder, ctx)

    if entries:
        from ..reports import entry_row
        items = []
        for e in entries:
            items.append("<li>" + entry_row(
                e["title"], relative_path(page_path, e["path"]),
                e.get("kind") or "document", e.get("updated_usec") or 0,
                color=e.get("color") or "") + "</li>")
        listing = ('<h2>Contents</h2>\n<ul class="quip-listing">\n'
                   + "\n".join(items) + "\n</ul>")
    else:
        listing = '<p class="quip-empty-folder">This folder is empty.</p>'

    body = "\n".join([
        icon_sprite(),
        site_chrome(page_path),
        crumbs,
        f"<h1>{H.escape_text(folder.display_title)}</h1>",
        meta,
        listing,
    ])
    return page(folder.display_title, body, css,
                body_class="quip-folder-page",
                extra_head=chrome_scripts(page_path),
                footer=footer(ctx.exported_at()))


def _metadata(folder: Folder, ctx) -> str:
    attrs = {
        "class": "quip-meta",
        "data-quip-folder-id": folder.id,
        "data-quip-folder-type": folder.folder_type,
        "data-quip-created-usec": str(folder.created_usec),
        "data-quip-updated-usec": str(folder.updated_usec),
        "data-quip-link": folder.link,
        "data-quip-secret-path": folder.secret_path,
    }
    attr_str = " ".join(f'{k}="{H.escape_attr(v)}"' for k, v in attrs.items() if v)

    lines = [f"<blockquote {attr_str}>",
             f'<div><span class="quip-meta-label">Created:</span> '
             f'{H.escape_text(format_date(folder.created_usec))}</div>']
    if folder.updated_usec:
        lines.append(f'<div><span class="quip-meta-label">Last modified:</span> '
                     f'{H.escape_text(format_date(folder.updated_usec))}</div>')
    lines.append(f'<div><span class="quip-meta-label">Access:</span> '
                 f'{ctx.describe_folder_sharing(folder)}</div>')
    if folder.link:
        lines.append(f'<div><span class="quip-meta-label">Imported from:</span> '
                     f'<a href="{H.escape_attr(folder.link)}" class="quip-source-link">'
                     f'{H.escape_text(folder.link)}</a></div>')
    if folder.restricted_child_ids:
        lines.append(f'<div><span class="quip-flag">'
                     f'{len(folder.restricted_child_ids)} subfolder(s) were restricted '
                     f'and could not be exported</span></div>')
    lines.append(f'<div class="quip-meta-ids"><span class="quip-meta-label">Quip ID:</span> '
                 f'{H.escape_text(folder.id)}</div>')
    lines.append("</blockquote>")
    return "\n".join(lines)


def _breadcrumbs(folder: Folder, ctx, page_path: str):
    parts = [("All documents", relative_path(page_path, "index.html"))]
    segs = folder.output_path.split("/")
    acc = []
    for seg in segs[:-1]:
        acc.append(seg)
        path = "/".join(acc)
        if path == ctx.content_dir_name:
            continue
        parts.append((seg, relative_path(page_path, f"{path}/index.html")))
    parts.append((folder.display_title, None))
    return breadcrumb(parts)
