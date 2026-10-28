"""A tolerant mini-DOM for Quip's HTML.

Quip returns HTML that is *not* XML: valueless attributes
(`data-live-app-process-bar-step-selected`) in both API versions, and bare
`<br>` in V2. `baqup` parses with `xml.etree` and fails on real documents.

This module is the only place that knows how HTML is parsed or serialized.
Stdlib only, so the irreplaceable phases need no virtualenv.

Design notes:
  * `convert_charrefs=False` so entities survive round-trip byte-for-byte.
  * Unmatched end tags are dropped; unclosed elements are closed at EOF.
    Losing content is never acceptable; losing well-formedness is.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Elements that never have children or a closing tag.
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class Node:
    parent = None

    def text(self) -> str:
        return ""

    def html(self) -> str:
        return ""


class Text(Node):
    """Raw source text, entities included, stored exactly as received."""

    __slots__ = ("data", "parent")

    def __init__(self, data: str):
        self.data = data
        self.parent = None

    def text(self) -> str:
        return unescape_text(self.data)

    def html(self) -> str:
        return self.data

    def __repr__(self):
        return f"Text({self.data[:30]!r})"


class Comment(Node):
    __slots__ = ("data", "parent")

    def __init__(self, data: str):
        self.data = data
        self.parent = None

    def html(self) -> str:
        return f"<!--{self.data}-->"


class Raw(Node):
    """Pre-rendered markup injected by us; emitted verbatim."""

    __slots__ = ("data", "parent")

    def __init__(self, data: str):
        self.data = data
        self.parent = None

    def text(self) -> str:
        return ""

    def html(self) -> str:
        return self.data


class Element(Node):
    __slots__ = ("tag", "attrs", "children", "parent", "self_closed")

    def __init__(self, tag: str, attrs=None, self_closed: bool = False):
        self.tag = tag
        # dict preserving order; value None means a valueless attribute
        self.attrs: dict[str, str | None] = dict(attrs or {})
        self.children: list[Node] = []
        self.parent = None
        self.self_closed = self_closed

    # -- attributes ------------------------------------------------------

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    def has(self, name) -> bool:
        return name in self.attrs

    def set(self, name, value):
        self.attrs[name] = value

    def classes(self) -> list[str]:
        return (self.get("class") or "").split()

    def add_class(self, name):
        cs = self.classes()
        if name not in cs:
            cs.append(name)
            self.attrs["class"] = " ".join(cs)

    # -- tree ------------------------------------------------------------

    def append(self, node: Node):
        node.parent = self
        self.children.append(node)
        return node

    def insert_before(self, ref: Node, node: Node) -> bool:
        try:
            i = self.children.index(ref)
        except ValueError:
            return False
        node.parent = self
        self.children.insert(i, node)
        return True

    def replace(self, ref: Node, *nodes: Node) -> bool:
        try:
            i = self.children.index(ref)
        except ValueError:
            return False
        for n in nodes:
            n.parent = self
        self.children[i : i + 1] = list(nodes)
        return True

    def remove(self, ref: Node) -> bool:
        try:
            self.children.remove(ref)
            return True
        except ValueError:
            return False

    # -- output ----------------------------------------------------------

    def text(self) -> str:
        return "".join(c.text() for c in self.children)

    def html(self) -> str:
        bits = [self.tag]
        for name, value in self.attrs.items():
            bits.append(name if value is None else f'{name}="{escape_attr(value)}"')
        open_tag = "<" + " ".join(bits)
        if self.tag in VOID:
            return open_tag + ">"
        if self.self_closed and not self.children:
            return open_tag + "></" + self.tag + ">"
        return open_tag + ">" + "".join(c.html() for c in self.children) + f"</{self.tag}>"

    def __repr__(self):
        ident = self.get("id") or self.get("class") or ""
        return f"<{self.tag} {ident}>"


class Fragment(Element):
    """Invisible root. Serializes as its children only."""

    def __init__(self):
        super().__init__("#fragment")

    def html(self) -> str:
        return "".join(c.html() for c in self.children)


# -- escaping ------------------------------------------------------------

def escape_attr(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def escape_text(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ENTITY_RE = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);")


def unescape_text(value: str) -> str:
    from html import unescape
    return unescape(value)


# -- parsing -------------------------------------------------------------

class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Fragment()
        self.stack: list[Element] = [self.root]

    @property
    def cur(self) -> Element:
        return self.stack[-1]

    def handle_starttag(self, tag, attrs):
        el = Element(tag, attrs)
        self.cur.append(el)
        if tag not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        el = Element(tag, attrs, self_closed=True)
        self.cur.append(el)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        # Close the nearest matching ancestor; ignore a stray end tag entirely
        # rather than letting it unwind the tree and orphan content.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.cur.append(Text(data))

    def handle_entityref(self, name):
        self.cur.append(Text(f"&{name};"))

    def handle_charref(self, name):
        self.cur.append(Text(f"&#{name};"))

    def handle_comment(self, data):
        self.cur.append(Comment(data))

    def handle_decl(self, decl):
        self.cur.append(Raw(f"<!{decl}>"))

    def unknown_decl(self, data):
        self.cur.append(Raw(f"<![{data}]>"))


def parse(html: str) -> Fragment:
    """Parse an HTML fragment into a tree. Never raises on malformed input."""
    b = _Builder()
    b.feed(html or "")
    b.close()
    return b.root


def serialize(node: Node) -> str:
    return node.html()


# -- traversal -----------------------------------------------------------

def walk(node: Node):
    """Depth-first over every node, including the root."""
    yield node
    if isinstance(node, Element):
        for child in list(node.children):
            yield from walk(child)


def iter_elements(node: Node, tag: str | None = None, predicate=None):
    for n in walk(node):
        if not isinstance(n, Element) or isinstance(n, Fragment):
            continue
        if tag is not None and n.tag != tag:
            continue
        if predicate is not None and not predicate(n):
            continue
        yield n


def find_by_id(node: Node, element_id: str) -> Element | None:
    for el in iter_elements(node):
        if el.get("id") == element_id:
            return el
    return None


def text_content(node: Node) -> str:
    return node.text()


def top_level_ancestor(el: Element, root: Element) -> Element:
    """The outermost element under `root` that contains `el` (for anchoring)."""
    cur, last = el, el
    while cur is not None and cur.parent is not None and cur.parent is not root:
        cur = cur.parent
        if isinstance(cur, Element):
            last = cur
    return last if cur is not None else el


def roundtrip_ok(html: str) -> tuple[bool, str]:
    """Verify parse->serialize preserves visible text. Used as a safety net."""
    tree = parse(html)
    out = serialize(tree)
    before = re.sub(r"\s+", " ", unescape_text(re.sub(r"<[^>]*>", "", html))).strip()
    after = re.sub(r"\s+", " ", tree.text()).strip()
    if before == after:
        return True, ""
    return False, f"text length {len(before)} -> {len(after)}, output {len(out)} bytes"
