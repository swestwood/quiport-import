"""Classifying Quip identifiers by shape.

Quip's 11-character permanent ids encode their entity type in a single
character. Verified empirically against 498 ids of independently known type
in the test account -- 325 users, 136 threads, 35 folders, 2 companies -- with
**no overlap** at any other position:

    index:  0 1 2 3 4 5 6 7 8 9 10
                    ^
    IVZ A E A fDxdt   E -> user
    AVN 9 A A eqq5w   A -> thread
    XTR A O A jWF4G   O -> folder
    NQH A c A V4DT7   c -> company

This is not documented, so it is treated as a strong hint rather than a
guarantee: it is used where a wrong answer is harmless (deciding that a link is
a person and should render as their name) and never where it would drop data.

Secret paths are a different namespace -- 12 characters, no type encoding -- so
they are explicitly excluded.
"""

from __future__ import annotations

import re

ID_LENGTH = 11
TYPE_INDEX = 4

TYPE_BY_CHAR = {
    "E": "user",
    "A": "thread",
    "O": "folder",
    "c": "company",
}

PERMANENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SECRET_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{12}$")


def id_type(token: str) -> str | None:
    """'user' | 'thread' | 'folder' | 'company', or None if not classifiable.

    Returns None for secret paths, malformed tokens, and any type character we
    have not verified -- an unknown character means we do not know, not that it
    is something else.
    """
    if not token or not PERMANENT_ID_RE.match(token):
        return None
    return TYPE_BY_CHAR.get(token[TYPE_INDEX])


def is_user_id(token: str) -> bool:
    return id_type(token) == "user"


def is_secret_path(token: str) -> bool:
    return bool(token and SECRET_PATH_RE.match(token))
