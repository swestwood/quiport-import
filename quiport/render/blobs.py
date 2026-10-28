"""Materialize downloaded blobs onto disk and hand back relative links.

Blob responses carry no usable type information: the API reports
`Content-Type: application/x-www-form-urlencoded` for PNGs and sends no
`Content-Disposition`. Extensions therefore come from the `alt` attribute in
the document HTML, or from magic bytes -- never from headers. (`baqup` trusts
`Content-Disposition` and would name every image `image.png`.)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..naming import relative_path, slugify

log = logging.getLogger(__name__)

# Enough signatures to cover what actually turns up in Quip documents.
MAGIC = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),          # also docx/xlsx/pptx
    (b"\x1a\x45\xdf\xa3", ".webm"),
    (b"\x00\x00\x00\x18ftyp", ".mp4"), (b"\x00\x00\x00\x20ftyp", ".mp4"),
    (b"RIFF", ".webp"),               # refined below
    (b"BM", ".bmp"),
    (b"<svg", ".svg"), (b"<?xml", ".svg"),
]

OFFICE_BY_ZIP_HINT = {"word/": ".docx", "xl/": ".xlsx", "ppt/": ".pptx"}


def sniff_extension(data: bytes, name_hint: str = "") -> str:
    """Extension from the filename hint if it has one, else from magic bytes."""
    if name_hint:
        m = re.search(r"(\.[A-Za-z0-9]{1,8})$", name_hint.strip())
        if m:
            return m.group(1).lower()

    head = data[:32]
    for sig, ext in MAGIC:
        if head.startswith(sig):
            if ext == ".webp" and b"WEBP" not in data[:16]:
                continue
            if ext == ".zip":
                window = data[:4000]
                for hint, office in OFFICE_BY_ZIP_HINT.items():
                    if hint.encode() in window:
                        return office
            return ext
    return ".bin"


class BlobStore:
    """Writes blobs under `_files/<secret_path>/` and resolves relative links."""

    def __init__(self, cfg, raw_store, content_dir_name: str):
        self.cfg = cfg
        self.raw = raw_store
        self.content_dir_name = content_dir_name
        self.files_rel = f"{content_dir_name}/_files"
        self._written: dict[tuple, str] = {}   # (thread,blob) -> export-relative path
        self.missing: set = set()
        self.count = 0
        self.xlsx_count = 0
        self.bytes = 0

    def resolver_for(self, thread):
        """A callable matching the signature the transforms expect."""
        def resolve(blob_thread_id, blob_id, element, page_path, name_hint=""):
            tid = blob_thread_id or thread.id
            hint = name_hint
            if not hint and element is not None:
                hint = element.get("alt") or element.get("title") or ""
                href = element.get("href") or ""
                m = re.search(r"[?&]name=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    hint = unquote(m.group(1))
            return self.materialize(tid, blob_id, thread, page_path, hint)
        return resolve

    def materialize_xlsx(self, thread, page_path) -> str | None:
        """Write the exported workbook beside the document and link to it.

        Named after the document rather than the thread id, because this is a
        file a human will download and open.
        """
        resp = self.raw.load("GET", f"/1/threads/{thread.id}/export/xlsx", {})
        if resp is None or not resp.ok or not resp.body.startswith(b"PK"):
            return None

        folder = thread.secret_path or thread.id
        stem = slugify(thread.display_title, 60) or "spreadsheet"
        rel_dir = f"{self.files_rel}/{folder}"
        out_path = self.cfg.root / rel_dir / f"{stem}.xlsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not out_path.exists():
            out_path.write_bytes(resp.body)
            self.xlsx_count += 1
            self.bytes += len(resp.body)
        export_rel = f"{rel_dir}/{stem}.xlsx"
        # Recorded on the thread so the reports can link it without having to
        # reconstruct the filename from the title.
        thread.xlsx_path = export_rel
        return relative_path(page_path, export_rel)

    def materialize(self, thread_id, blob_id, thread, page_path, name_hint="") -> str | None:
        """Write the blob if we have it; return a link relative to `page_path`."""
        key = (thread_id, blob_id)
        if key in self.missing:
            return None
        if key in self._written:
            return relative_path(page_path, self._written[key])

        resp = self.raw.load("GET", f"/1/blob/{thread_id}/{blob_id}", {})
        if resp is None or not resp.ok or resp.body.lstrip().startswith(b'{"error'):
            status = resp.status if resp is not None else "not fetched"
            log.debug("blob %s/%s unavailable (%s)", thread_id, blob_id, status)
            self.missing.add(key)
            return None

        ext = sniff_extension(resp.body, name_hint)
        stem = slugify(Path(name_hint).stem, 50) if name_hint else ""
        filename = f"{stem}-{blob_id[:8]}{ext}" if stem else f"{blob_id}{ext}"

        folder = thread.secret_path or thread.id
        rel_dir = f"{self.files_rel}/{folder}"
        out_path = self.cfg.root / rel_dir / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not out_path.exists():
            out_path.write_bytes(resp.body)
            self.count += 1
            self.bytes += len(resp.body)

        export_rel = f"{rel_dir}/{filename}"
        self._written[key] = export_rel
        return relative_path(page_path, export_rel)
