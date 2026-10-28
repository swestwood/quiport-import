"""Phase 4 -- rewrite Quip URLs in the rendered HTML to relative local paths.

Runs after rendering, when every document's final location is known. Uses the
many-to-one alias index from `id-map.json`, so links carrying an older secret
path still resolve.

Person and date @mentions become plain text, per the spec. Anything that cannot
be resolved keeps its absolute Quip URL and is marked `quip-unresolved` so it
stays findable.
"""

from __future__ import annotations

import json
import logging
import re

from . import htmlutil as H
from .naming import relative_path
from .quipid import is_user_id

log = logging.getLogger(__name__)

QUIP_URL_RE = re.compile(
    r"^https?://([\w.-]*\.)?quip\.com/([\w-]+)(?:/([^?#]*))?(\?[^#]*)?(#.*)?$", re.I)

# Thread ids are 11 characters and secret paths 12, both mixed-case
# alphanumeric. quip.com also serves ordinary marketing pages -- /about, /chat,
# /pricing -- which are external links, not broken document references.
ID_SHAPE_RE = re.compile(r"^(?=.*[A-Z])(?=.*[a-z0-9])[A-Za-z0-9_-]{10,32}$")


def looks_like_id(token: str) -> bool:
    return bool(ID_SHAPE_RE.match(token or ""))


class LinkIndex:
    def __init__(self, id_map: dict):
        self.threads = id_map.get("threads", {})
        self.folders = id_map.get("folders", {})
        self.aliases = id_map.get("aliases", {})

    def resolve(self, token: str) -> tuple[str, str] | None:
        """token (thread id, secret path, or folder id) -> (path, title)."""
        thread_id = self.aliases.get(token, token)
        t = self.threads.get(thread_id)
        if t and t.get("path"):
            return t["path"], t.get("title", "")
        f = self.folders.get(thread_id) or self.folders.get(token)
        if f and f.get("path"):
            return f["path"], f.get("title", "")
        return None


def run(cfg, user_ids=None) -> dict:
    id_map_path = cfg.root / "id-map.json"
    if not id_map_path.exists():
        raise RuntimeError("id-map.json missing: run render first")
    index = LinkIndex(json.loads(id_map_path.read_text(encoding="utf-8")))
    people = set(user_ids or ())

    stats = {"files": 0, "rewritten": 0, "unresolved": 0, "mentions": 0}
    for path in sorted(cfg.root.rglob("*.html")):
        rel = path.relative_to(cfg.root).as_posix()
        try:
            html = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_html, changed = _rewrite_file(html, rel, index, people, stats)
        if changed:
            path.write_text(new_html, encoding="utf-8")
        stats["files"] += 1

    log.info("Link pass: %s", stats)
    return stats


def _rewrite_file(html: str, page_path: str, index: LinkIndex, people: set, stats) -> tuple[str, bool]:
    tree = H.parse(html)
    changed = False

    for a in list(H.iter_elements(tree, "a")):
        href = (a.get("href") or "").strip()

        # The metadata "Imported from" link is the one pointer back to the
        # original in Quip. Rewriting it to a local file would destroy the only
        # reference the archive keeps to where it came from.
        if "quip-source-link" in a.classes():
            stats["preserved"] = stats.get("preserved", 0) + 1
            continue

        # Live apps embed root-relative Salesforce URLs (/lightning/r/...) that
        # only resolved against the Salesforce host. Locally they point at the
        # filesystem root and go nowhere, so mark them rather than leave a link
        # that looks live but is not.
        if href.startswith("/") and not href.startswith("//"):
            if "quip-external-app-link" not in a.classes():
                a.add_class("quip-external-app-link")
                a.set("title", "This link pointed at an external application "
                                "and does not resolve in the archive.")
                changed = True
                stats["external_app"] = stats.get("external_app", 0) + 1
            continue

        m = QUIP_URL_RE.match(href)
        if not m:
            continue
        token = m.group(2)
        fragment = m.group(5) or ""

        # quip.com/about and friends are ordinary web pages: leave them alone.
        if not looks_like_id(token):
            stats["external"] = stats.get("external", 0) + 1
            continue

        # A mention of a person is just their name, per the spec.
        #
        # Two independent signals, because either alone leaves gaps: the id
        # shape (character 4 is "E" for a user) catches deactivated accounts the
        # users endpoint rejects, and the resolved-name set catches anything the
        # shape rule cannot classify. The anchor text is already the person's
        # name as Quip rendered it, so no lookup is needed to display it.
        if token in people or is_user_id(token):
            text = a.text().strip() or token
            strong = H.Element("strong", {"class": "quip-mention-person"})
            strong.append(H.Text(H.escape_text(text.lstrip("@"))))
            if isinstance(a.parent, H.Element):
                a.parent.replace(a, strong)
                changed = True
                stats["mentions"] += 1
            continue

        hit = index.resolve(token)
        if hit:
            target, title = hit
            a.set("href", relative_path(page_path, target) + fragment)
            a.add_class("quip-internal-link")
            if not a.text().strip() and title:
                a.append(H.Text(H.escape_text(title)))
            changed = True
            stats["rewritten"] += 1
        else:
            a.add_class("quip-unresolved")
            a.set("data-quip-unresolved-id", token)
            changed = True
            stats["unresolved"] += 1

    return (H.serialize(tree), True) if changed else (html, False)
