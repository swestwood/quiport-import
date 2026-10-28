"""Normalized views over the raw API records.

The API is inconsistent between versions -- V1 says `type: "document"`, V2 says
`type: "DOCUMENT"`; V2's bulk response nests under `thread` despite its docs
showing it flat. Every one of those quirks is absorbed here so the rest of the
codebase sees one shape.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# thread_class values that mean "this is a conversation, not a document"
CHAT_CLASSES = frozenset({"channel", "two_person_chat", "chat"})


def usec_to_datetime(usec) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(usec) / 1_000_000, tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def format_date(usec) -> str:
    d = usec_to_datetime(usec)
    return d.strftime("%B %-d, %Y") if d else "unknown"


def format_datetime(usec) -> str:
    d = usec_to_datetime(usec)
    return d.strftime("%b %-d, %Y") if d else "unknown"


def format_exact(usec) -> str:
    """Full timestamp for a tooltip: "March 9, 2021 at 2:34 pm".

    The visible date is deliberately friendly; this keeps the precision the
    friendly form drops, without spending a line on it.
    """
    d = usec_to_datetime(usec)
    if d is None:
        return ""
    return (d.astimezone().strftime("%B %-d, %Y at %-I:%M %p")
            .replace("AM", "am").replace("PM", "pm"))


def format_export_time(iso: str) -> str:
    """'2026-08-16T22:17:43+00:00' -> 'Aug 16, 2026 at 10:17 pm' in local time."""
    if not iso:
        return ""
    try:
        stamp = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return ""
    return stamp.strftime("%b %-d, %Y at %-I:%M %p").replace("AM", "am").replace("PM", "pm")


def unwrap(record):
    """V1 and V2 bulk records both nest the payload under `thread`."""
    if isinstance(record, dict) and "thread" in record and isinstance(record["thread"], dict):
        return record["thread"]
    return record if isinstance(record, dict) else {}


@dataclass
class Thread:
    id: str
    secret_path: str = ""
    title: str = ""
    type: str = "document"          # normalized lowercase
    thread_class: str = ""
    created_usec: int = 0
    updated_usec: int = 0
    link: str = ""
    author_id: str = ""
    is_deleted: bool = False
    is_template: bool = False
    spreadsheet_summary: dict | None = None
    sharing: dict = field(default_factory=dict)

    # sharing / membership
    expanded_user_ids: list = field(default_factory=list)
    access_levels: dict = field(default_factory=dict)
    member_ids: list = field(default_factory=list)
    invited_emails: list = field(default_factory=list)
    folder_ids: list = field(default_factory=list)

    # content
    html: str = ""                  # V2 -- authoritative
    html_complete: bool = True      # False when the cursor chain never terminated
    html_pages: int = 0
    v1_html: str = ""               # V1 -- live-app names only, never rendered
    messages: list = field(default_factory=list)

    # export bookkeeping
    folder_paths: list = field(default_factory=list)
    output_path: str = ""
    xlsx_path: str = ""             # export-root-relative, set when rendered
    errors: list = field(default_factory=list)

    # -- classification --------------------------------------------------

    @property
    def is_chat(self) -> bool:
        return self.type == "chat" or self.thread_class in CHAT_CLASSES

    @property
    def is_slides(self) -> bool:
        return self.type == "slides"

    @property
    def is_spreadsheet(self) -> bool:
        return self.type == "spreadsheet"

    @property
    def has_embedded_spreadsheet(self) -> bool:
        """A document carrying spreadsheet_summary has a spreadsheet inside it.

        The HTML flattens these to plain <table> with no sign that live
        spreadsheet semantics were lost, so they need human review.
        """
        return bool(self.spreadsheet_summary) and not self.is_spreadsheet

    @property
    def has_formulas(self) -> bool:
        return bool((self.spreadsheet_summary or {}).get("has_formulas"))

    @property
    def has_charts(self) -> bool:
        return bool((self.spreadsheet_summary or {}).get("has_charts"))

    @property
    def display_title(self) -> str:
        return (self.title or "").strip() or "Untitled"

    # -- construction ----------------------------------------------------

    @classmethod
    def from_raw(cls, thread_id: str, v1: dict | None, v2: dict | None) -> "Thread":
        v1 = v1 or {}
        v2 = v2 or {}
        t1 = unwrap(v1)
        t2 = unwrap(v2)

        # V2 is more current; V1 fills the gaps it does not expose.
        merged = {**t1, **{k: v for k, v in t2.items() if v is not None}}
        link = merged.get("link", "") or ""
        secret = t2.get("secret_path") or link.rstrip("/").rsplit("/", 1)[-1]

        return cls(
            id=merged.get("id") or thread_id,
            secret_path=secret or "",
            title=merged.get("title", "") or "",
            type=(merged.get("type") or "document").lower(),
            thread_class=(t1.get("thread_class") or "").lower(),
            created_usec=merged.get("created_usec") or 0,
            updated_usec=merged.get("updated_usec") or 0,
            link=link,
            author_id=merged.get("author_id", "") or "",
            is_deleted=bool(t1.get("is_deleted")),
            is_template=bool(merged.get("is_template")),
            spreadsheet_summary=t2.get("spreadsheet_summary"),
            sharing=t2.get("sharing") or {},
            expanded_user_ids=list(v1.get("expanded_user_ids") or []),
            access_levels=dict(v1.get("access_levels") or {}),
            invited_emails=list(v1.get("invited_user_emails") or []),
            folder_ids=list(v1.get("shared_folder_ids") or []),
            v1_html=v1.get("html") or "",
        )


@dataclass
class Folder:
    id: str
    title: str = ""
    folder_type: str = ""           # private | shared | group ...
    color: str = ""                 # manila | blue | light_blue | ... (Quip's own names)
    created_usec: int = 0
    updated_usec: int = 0
    link: str = ""
    creator_id: str = ""
    member_ids: list = field(default_factory=list)
    child_thread_ids: list = field(default_factory=list)
    child_folder_ids: list = field(default_factory=list)
    restricted_child_ids: list = field(default_factory=list)
    parent_id: str = ""
    output_path: str = ""
    path_parts: list = field(default_factory=list)

    @property
    def secret_path(self) -> str:
        return (self.link or "").rstrip("/").rsplit("/", 1)[-1]

    @property
    def is_private(self) -> bool:
        return self.folder_type == "private"

    @property
    def display_title(self) -> str:
        return (self.title or "").strip() or "Untitled Folder"

    @classmethod
    def from_raw(cls, folder_id: str, record: dict) -> "Folder":
        f = (record or {}).get("folder") or {}
        threads, folders, restricted = [], [], []
        for child in (record or {}).get("children", []) or []:
            if "thread_id" in child:
                threads.append(child["thread_id"])
            elif "folder_id" in child:
                (restricted if child.get("restricted") else folders).append(child["folder_id"])
        return cls(
            id=f.get("id") or folder_id,
            title=f.get("title", "") or "",
            folder_type=(f.get("folder_type") or "").lower(),
            color=(f.get("color") or "").lower(),
            created_usec=f.get("created_usec") or 0,
            updated_usec=f.get("updated_usec") or 0,
            link=f.get("link", "") or "",
            creator_id=f.get("creator_id", "") or "",
            member_ids=list((record or {}).get("member_ids") or []),
            child_thread_ids=threads,
            child_folder_ids=folders,
            restricted_child_ids=restricted,
        )


@dataclass
class Message:
    id: str
    author_id: str = ""
    author_name: str = ""
    created_usec: int = 0
    updated_usec: int = 0
    text: str = ""
    visible: bool = True
    annotation_id: str = ""
    highlight_section_ids: list = field(default_factory=list)
    files: list = field(default_factory=list)
    parts: list = field(default_factory=list)
    mention_user_ids: list = field(default_factory=list)

    @property
    def is_comment(self) -> bool:
        """Anchored comment vs document-level chat message."""
        return bool(self.annotation_id)

    @classmethod
    def from_raw(cls, record: dict) -> "Message":
        ann = record.get("annotation") or {}
        return cls(
            id=record.get("id", ""),
            author_id=record.get("author_id", "") or "",
            author_name=record.get("author_name", "") or "",
            created_usec=record.get("created_usec") or 0,
            updated_usec=record.get("updated_usec") or 0,
            text=record.get("text", "") or "",
            visible=record.get("visible", True) is not False,
            annotation_id=ann.get("id", "") or "",
            # NOTE: the docs say `highlighted_section_ids`; the API returns
            # `highlight_section_ids`. Accept both, prefer what is real.
            highlight_section_ids=list(
                ann.get("highlight_section_ids") or ann.get("highlighted_section_ids") or []
            ),
            files=list(record.get("files") or []),
            parts=list(record.get("parts") or []),
            mention_user_ids=list(record.get("mention_user_ids") or []),
        )
