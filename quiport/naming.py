"""Filesystem naming and canonical path allocation.

Documents are named `SECRETPATH-Title-Made-URL-Safe.html` so that a future tool
can take a `quip.com/SECRETPATH/Title` link and find the file by prefix. The
secret-path prefix also makes collisions impossible, which matters because Quip
titles are neither unique nor required to be non-empty.
"""

from __future__ import annotations

import re
import unicodedata

MAX_SLUG = 80
# Reserved on Windows; harmless to avoid everywhere.
RESERVED = frozenset("""
    CON PRN AUX NUL COM1 COM2 COM3 COM4 COM5 COM6 COM7 COM8 COM9
    LPT1 LPT2 LPT3 LPT4 LPT5 LPT6 LPT7 LPT8 LPT9
""".split())


def slugify(text: str, max_len: int = MAX_SLUG) -> str:
    """ASCII-fold, collapse to hyphens, bound the length."""
    if not text:
        return ""
    # Keep the readable shape of accented characters rather than dropping them.
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) > max_len:
        # Cut on a hyphen boundary when one is close, so words stay whole.
        cut = text[:max_len]
        if "-" in cut[max_len // 2 :]:
            cut = cut[: cut.rindex("-")]
        text = cut.strip("-")
    if text.upper() in RESERVED:
        text += "-doc"
    return text


def thread_filename(secret_path: str, title: str, ext: str = ".html") -> str:
    """`Sp1xmplPath0-Weekly-Sales-Email-Plan.html`"""
    slug = slugify(title) or "untitled"
    prefix = re.sub(r"[^\w-]", "", secret_path or "")
    return f"{prefix}-{slug}{ext}" if prefix else f"{slug}{ext}"


def folder_dirname(title: str) -> str:
    return slugify(title, max_len=60) or "untitled-folder"


def relative_path(from_path: str, to_path: str) -> str:
    """Relative link between two export-root-relative POSIX paths."""
    if not to_path:
        return ""
    from_parts = [p for p in from_path.split("/") if p][:-1]  # drop filename
    to_parts = [p for p in to_path.split("/") if p]
    i = 0
    while i < len(from_parts) and i < len(to_parts) - 1 and from_parts[i] == to_parts[i]:
        i += 1
    up = [".."] * (len(from_parts) - i)
    rel = "/".join(up + to_parts[i:])
    return rel or to_parts[-1]


def path_to_root(from_path: str) -> str:
    """Prefix that walks from a page back to the export root.

    `Imported from Quip/A/B/index.html` -> `../../../`, and `index.html` -> ``.
    Used for assets that live at the root (icons, the shared JS) but are
    referenced from pages at any depth.
    """
    depth = len([p for p in (from_path or "").split("/") if p]) - 1
    return "../" * max(depth, 0)


class PathAllocator:
    """Assigns one canonical output path per thread, plus stub locations.

    Quip folders are tags, not directories: a thread can live in several. The
    first folder that claims a thread owns the real file; every other location
    gets a stub that links to it. Walk order is deterministic so reruns agree.
    """

    def __init__(self, content_dir_name: str, max_len: int = MAX_SLUG):
        self.content_dir_name = content_dir_name
        self.max_len = max_len
        self.canonical: dict[str, str] = {}     # thread_id -> path
        self.stubs: dict[str, list[str]] = {}   # thread_id -> [paths]
        self.folder_paths: dict[str, str] = {}  # folder_id -> dir path
        self._taken: set[str] = set()

    def allocate_folder(self, folder_id: str, title: str, parent_id: str | None) -> str:
        if folder_id in self.folder_paths:
            return self.folder_paths[folder_id]
        parent = self.folder_paths.get(parent_id, self.content_dir_name) if parent_id \
            else self.content_dir_name
        name = folder_dirname(title)
        path = f"{parent}/{name}"
        # Sibling folders can share a title; disambiguate with the id.
        if path.lower() in self._taken:
            path = f"{parent}/{name}-{folder_id[:6]}"
        self._taken.add(path.lower())
        self.folder_paths[folder_id] = path
        return path

    def allocate_thread(self, thread_id: str, secret_path: str, title: str,
                        folder_id: str | None) -> tuple[str, bool]:
        """Returns (path, is_canonical)."""
        folder_path = self.folder_paths.get(folder_id, self.content_dir_name)
        filename = thread_filename(secret_path, title)
        path = f"{folder_path}/{filename}"

        if thread_id not in self.canonical:
            self.canonical[thread_id] = path
            self._taken.add(path.lower())
            return path, True

        if path != self.canonical[thread_id]:
            self.stubs.setdefault(thread_id, [])
            if path not in self.stubs[thread_id]:
                self.stubs[thread_id].append(path)
        return path, False

    def all_paths_for(self, thread_id: str) -> list[str]:
        paths = []
        if thread_id in self.canonical:
            paths.append(self.canonical[thread_id])
        paths.extend(self.stubs.get(thread_id, []))
        return paths
