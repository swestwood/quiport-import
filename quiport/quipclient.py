"""Read-only, rate-limit-aware Quip API client.

Deliberately has no POST/PUT/DELETE surface: a write is not merely discouraged,
it is unwriteable through this class.

Stdlib only, so it runs anywhere without a virtualenv.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://platform.quip.com"

# Statuses that mean "the server is throttling us, wait and try again forever".
THROTTLE_STATUSES = frozenset({429, 503})
# Statuses worth a bounded retry. 504 is documented only on /2/threads/{id}/html,
# which is our sole content source, so it must not be treated as fatal.
TRANSIENT_STATUSES = frozenset({500, 502, 504})

# Ceiling on a single throttled sleep.
#
# Quip enforces more than one user quota and the headers report whichever is
# currently binding. Both of these were observed on the *same* account:
#
#   X-Ratelimit-Limit: 50    resets within 60s      (per-minute)
#   X-Ratelimit-Limit: 750   resets in up to ~2550s (per-hour)
#
# So a block really can outlast an hour-long window, and an earlier revision of
# this comment claiming "no observed quota takes longer than a minute to refill"
# was wrong -- it had only seen the per-minute one. The cap is a responsiveness
# choice, not a model of the quota: sleeping in bounded steps keeps progress
# visible and Ctrl-C prompt, at the cost of a few refused calls per hour-long
# block. It is not a budget the retry loop can exhaust; throttles retry forever.
MAX_THROTTLE_SLEEP = 300
# How much each successive refusal of the same call adds to the wait.
THROTTLE_STEP = 20
# When to stop narrating individual sleeps and explain what is being waited on.
THROTTLE_HINT_AFTER = 4

# Statuses that are a real **answer about this resource**: the server told us
# something true and asking again will not change it. These are safe to cache and
# trust forever.
ANSWER_STATUSES = frozenset({400, 403, 404, 410})

# Everything else -- 0 (never reached the server), 5xx, 429 -- is a *failure to
# obtain an answer*. Caching one as though it were an answer is how a network
# blip permanently loses a document, so `RawStore.load` refuses to serve them.
# 401 is deliberately absent: it is a fact about the token, not the resource.


def is_answer(status: int) -> bool:
    """Did the server actually tell us something about this resource?"""
    return (200 <= status < 300) or status in ANSWER_STATUSES


# Measured on the live API: a continuation cursor is a *temporary* handle.
#
#   {"error_code":400,"error_description":"invalid or expired cursor: QWJH..."}
#
# It also turns out to be deterministic -- the base64 decodes to the id of the
# last item on the previous page -- so re-walking from page one produces the
# same, still-expired cursor. That combination is a trap: cached as a 400 (a
# normal "answer"), it makes any interrupted multi-page fetch permanently
# unfinishable, and the export would quietly keep the first hundred members of a
# thread as though they were all of them.
#
# So this particular 400 is a fact about the cursor, never about the resource.
EXPIRED_CURSOR_RE = re.compile(rb"invalid or expired cursor", re.IGNORECASE)


def is_expired_cursor(status: int, body: bytes) -> bool:
    return status == 400 and bool(EXPIRED_CURSOR_RE.search(body or b""))


def _require_https(base_url: str) -> str:
    """Refuse to send a bearer token over an unencrypted connection.

    `--base-url` exists for VPC customers, and a typo or a copied `http://` URL
    would put the token -- and every document the account contains -- on the
    wire in plaintext. There is no legitimate reason for this API to be reached
    over http, so it is an error rather than a warning.
    """
    url = (base_url or "").strip()
    if not url.lower().startswith("https://"):
        raise ValueError(
            f"base_url must be https:// -- got {url!r}. The token is sent as a "
            "bearer credential on every request; over http it, and everything "
            "it can read, would travel in plaintext.")
    return url


class QuipError(Exception):
    def __init__(self, status: int, endpoint: str, body: bytes = b""):
        self.status = status
        self.endpoint = endpoint
        self.body = body
        super().__init__(f"HTTP {status} for {endpoint}: {body[:200]!r}")


class QuipAuthError(QuipError):
    """The token is not usable. Never a fact about one resource.

    Raised rather than returned so it cannot be mistaken for a per-thread
    failure, cached, and applied to the rest of the account -- an expiring token
    would otherwise mark every remaining thread as an error and exit zero.
    """


@dataclass
class Response:
    """A single HTTP result, whether it came from the network or a cache."""

    status: int
    headers: dict
    body: bytes
    endpoint: str
    params: dict = field(default_factory=dict)
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class RateLimiter:
    """Self-throttles from the rate-limit headers the API actually returns.

    The documented per-user limit is 50/min, but real accounts report other
    values (the test account reports 100), so we trust headers over docs and
    fall back to a conservative floor when headers are absent.
    """

    def __init__(self, safety_margin: int = 5, fallback_per_min: int = 45):
        self.safety_margin = safety_margin
        self.fallback_per_min = fallback_per_min
        self.limit = None
        self.remaining = None
        self.reset_at = None
        self.company_remaining = None
        self.company_reset_at = None
        self._recent = []

    @staticmethod
    def _int(headers, name):
        """Case-insensitively, because HTTP header names are.

        Quip sends `X-Company-Ratelimit-Remaining` -- lowercase `l` -- while
        the per-user headers use `X-Ratelimit-*`. Matching only the exact
        spelling and its all-lowercase form missed the company pool entirely,
        so the limiter never paced against it and only found out it was
        exhausted by being refused.
        """
        if not headers:
            return None
        raw = headers.get(name)
        if raw is None:
            want = name.lower()
            for k, v in headers.items():
                if k.lower() == want:
                    raw = v
                    break
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def observe(self, headers: dict) -> None:
        self.limit = self._int(headers, "X-Ratelimit-Limit") or self.limit
        self.remaining = self._int(headers, "X-Ratelimit-Remaining")
        self.reset_at = self._int(headers, "X-Ratelimit-Reset") or self.reset_at
        self.company_remaining = self._int(headers, "X-Company-RateLimit-Remaining")
        self.company_reset_at = self._int(headers, "X-Company-RateLimit-Reset")

    def describe(self) -> str:
        """What the limiter currently believes, for the throttling log line."""
        now = time.time()
        parts = []
        for label, remaining, reset in (
            ("user", self.remaining, self.reset_at),
            ("company", self.company_remaining, self.company_reset_at),
        ):
            if remaining is None and reset is None:
                continue
            bit = f"{label} {remaining if remaining is not None else '?'}"
            if self.limit and label == "user":
                bit += f"/{self.limit}"
            if reset:
                bit += f", resets in {max(0, reset - now):.0f}s"
            parts.append(bit)
        return "; ".join(parts) or "no rate headers seen"

    def is_exhausted(self, headers: dict | None = None) -> bool:
        """Do the headers say a quota is actually spent?

        503 is both Quip's throttling signal and the generic "service
        unavailable". A refusal while every pool still reports requests
        remaining is the second kind, and retrying *that* forever would wedge
        the whole harvest on one broken resource.
        """
        for name in ("X-Ratelimit-Remaining", "X-Company-Ratelimit-Remaining"):
            if self._int(headers, name) == 0:
                return True
        for name in ("Retry-After", "X-Company-Retry-After"):
            if self._int(headers, name):
                return True
        # No usable headers: assume a quota, since retrying is the safe error.
        return not any(self._int(headers, n) is not None for n in (
            "X-Ratelimit-Remaining", "X-Company-Ratelimit-Remaining"))

    def sleep_until_reset(self, headers: dict | None = None) -> float:
        """Seconds to wait after being throttled. Prefers the server's own reset time."""
        reset = None
        if headers:
            reset = self._int(headers, "X-Ratelimit-Reset") or self._int(
                headers, "X-Company-RateLimit-Reset"
            )
            retry_after = self._int(headers, "X-Company-Retry-After") or self._int(
                headers, "Retry-After"
            )
            if retry_after:
                return float(min(retry_after, MAX_THROTTLE_SLEEP))
        reset = reset or self.reset_at
        if reset:
            return float(max(1.0, min(reset - time.time() + 1,
                                      MAX_THROTTLE_SLEEP)))
        return 30.0

    def wait_if_needed(self) -> None:
        """Pause before a request if we're close to a ceiling."""
        now = time.time()
        for pool, reset in (
            (self.remaining, self.reset_at),
            (self.company_remaining, self.company_reset_at),
        ):
            if pool is not None and pool <= self.safety_margin and reset:
                delay = reset - now + 1
                if delay > 0:
                    delay = min(delay, MAX_THROTTLE_SLEEP)
                    # Capped for the same reason as a throttled retry: every
                    # measured pool refills within a minute, so a longer sleep
                    # is a stall, not patience. If a reset really is further
                    # out, we retry -- visibly -- rather than going quiet.
                    log.info("Rate limit nearly exhausted (%s); sleeping %.0fs",
                             self.describe(), delay)
                    time.sleep(delay)
                    return

        # No headers seen yet: pace ourselves against a conservative floor.
        if self.remaining is None:
            self._recent = [t for t in self._recent if now - t < 60]
            if len(self._recent) >= self.fallback_per_min:
                delay = max(0.0, 60 - (now - self._recent[0])) + 0.5
                # A silent sleep here is indistinguishable from a hang.
                if delay >= 2:
                    log.info("Pacing against the %d/min floor; sleeping %.1fs",
                             self.fallback_per_min, delay)
                time.sleep(delay)
            self._recent.append(time.time())


class QuipClient:
    """GET-only Quip API client.

    If `store` is provided it must expose `load(method, endpoint, params)` and
    `save(method, endpoint, params, status, headers, body)`; every request and
    response then round-trips through it, which is what makes the export
    re-renderable offline.
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        store=None,
        timeout: int = 120,
        max_transient_retries: int = 6,
        user_agent: str = "quiport/1.0 (read-only archival export)",
    ):
        self.token = token
        self.base_url = _require_https(base_url).rstrip("/")
        self.store = store
        self.timeout = timeout
        self.max_transient_retries = max_transient_retries
        self.user_agent = user_agent
        self.limiter = RateLimiter()
        self.stats = {"network": 0, "cache": 0, "throttled": 0, "errors": 0}

    # -- public ----------------------------------------------------------

    def get(self, endpoint: str, allow_fetch: bool = True, force: bool = False,
            **params) -> Response:
        """Fetch `endpoint`, preferring the cache. Returns errors as Responses.

        `force=True` skips the cache *read* but still writes the result, for the
        case where a cached response is known to be unusable -- a page-1 body
        whose cursor has since expired.
        """
        params = {k: v for k, v in params.items() if v is not None}

        if self.store is not None and not force:
            cached = self.store.load("GET", endpoint, params)
            if cached is not None:
                self.stats["cache"] += 1
                cached.from_cache = True
                return cached

        if not allow_fetch:
            raise QuipError(0, endpoint, b"not in cache and fetching is disabled")

        resp = self._fetch(endpoint, params)
        if self.store is not None:
            self.store.save("GET", endpoint, params, resp.status, resp.headers, resp.body)
        return resp

    def get_json(self, endpoint: str, allow_fetch: bool = True, **params):
        """Convenience wrapper. Returns None for terminal errors rather than raising."""
        resp = self.get(endpoint, allow_fetch=allow_fetch, **params)
        if not resp.ok:
            return None
        try:
            return resp.json()
        except (ValueError, UnicodeDecodeError):
            log.warning("Non-JSON body from %s", endpoint)
            return None

    def paginate_all(self, endpoint: str, **params) -> tuple[list, bool]:
        """Every page of a cursor-paginated endpoint, and whether it finished.

        Returns `(pages, complete)`. `complete` is False if any page after the
        first failed to arrive, which the caller must treat as "this resource is
        not fully fetched" rather than "this is all of it".

        **Self-heals a stale cursor.** A cursor is a temporary handle. Recorded
        before an interruption and replayed hours later, the server can reject it
        -- and because page 1 is cached, its stale cursor would be replayed
        forever, so the chain could never finish. On a mid-chain failure the walk
        is retried once from page 1 with the cache bypassed, which yields fresh
        cursors. Without this, an interrupted harvest of a long document can
        never converge.
        """
        pages, complete = self._walk(endpoint, params, force=False)
        if not complete and pages:
            # Every page, not just the first: cursors are deterministic, so
            # page 2's cache key is the same one holding the expired-cursor 400.
            log.warning("%s: chain broke after page %d; re-walking from the "
                        "start, bypassing the cache", endpoint, len(pages))
            pages, complete = self._walk(endpoint, params, force=True)
        return pages, complete

    def _walk(self, endpoint: str, params: dict, force: bool) -> tuple[list, bool]:
        pages: list = []
        cursor = None
        while True:
            resp = self.get(endpoint, cursor=cursor, force=force, **params)
            if not resp.ok:
                if pages:
                    return pages, False        # chain broke part-way through
                # Nothing arrived at all. A 403/404 is the server's final word,
                # so there is nothing further to fetch and the walk *is* done;
                # a 5xx or a dropped connection is not, and must stay unfinished
                # so the caller retries rather than recording an empty result.
                return pages, is_answer(resp.status)
            try:
                page = resp.json()
            except (ValueError, UnicodeDecodeError):
                log.warning("%s: page %d is not JSON", endpoint, len(pages) + 1)
                return pages, False
            if not isinstance(page, dict):
                return pages, True             # a bare list has no cursor
            pages.append(page)
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return pages, True
            if len(pages) > 10_000:
                log.error("Runaway pagination on %s after %d pages", endpoint, len(pages))
                return pages, False

    # There is deliberately no `paginate()` generator any more. It ended the
    # chain on a failed page exactly as it did on a finished one, so callers
    # could not tell a complete result from a truncated one -- the bug class
    # `paginate_all` exists to remove. Keeping it around as a convenience would
    # be an invitation to reintroduce it.

    # -- internals -------------------------------------------------------

    def _fetch(self, endpoint: str, params: dict) -> Response:
        url = self.base_url + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        attempt = 0
        throttled = 0
        throttled_for = 0.0
        while True:
            self.limiter.wait_if_needed()
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "User-Agent": self.user_agent,
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as f:
                    body = f.read()
                    headers = dict(f.headers)
                    self.limiter.observe(headers)
                    self.stats["network"] += 1
                    return Response(f.status, headers, body, endpoint, params)

            except urllib.error.HTTPError as e:
                headers = dict(e.headers or {})
                body = e.read() if hasattr(e, "read") else b""
                self.limiter.observe(headers)

                if e.code == 401:
                    # Stop the whole run. Continuing would cache a 401 against
                    # every remaining resource and report success.
                    raise QuipAuthError(401, endpoint, body)

                # 503 means both "you are being throttled" and "this resource
                # is unavailable". Retrying the second kind forever wedges the
                # entire harvest on one broken blob, and it never returns to the
                # caller that knows how to record it and move on. The pools tell
                # them apart.
                if (e.code in THROTTLE_STATUSES
                        and not self.limiter.is_exhausted(headers)
                        and e.code not in (429,)):
                    if attempt < self.max_transient_retries:
                        attempt += 1
                        delay = min(2**attempt, 60)
                        log.warning(
                            "%d on %s with quota remaining (%s) -- treating as a "
                            "service error, not throttling; retry %d/%d in %ds",
                            e.code, endpoint, self.limiter.describe(),
                            attempt, self.max_transient_retries, delay)
                        time.sleep(delay)
                        continue
                    log.warning(
                        "%d on %s persisted through %d retries with quota "
                        "remaining; recording it and moving on rather than "
                        "blocking the run", e.code, endpoint,
                        self.max_transient_retries)
                    self.stats["errors"] += 1
                    return Response(e.code, headers, body, endpoint, params)

                if e.code in THROTTLE_STATUSES:
                    self.stats["throttled"] += 1
                    throttled += 1
                    delay = self.limiter.sleep_until_reset(headers)
                    # `Retry-After` describes the window the server happens to
                    # be reporting. If a *longer* pool is the real block -- the
                    # company quota outliving the per-minute one -- honouring it
                    # literally means waking up, being refused again, and
                    # sleeping the same 30s forever on the same request. Widen
                    # the wait each time the same call is refused.
                    if throttled > 1:
                        delay = max(delay, min(THROTTLE_STEP * throttled,
                                               MAX_THROTTLE_SLEEP))
                    throttled_for += delay
                    log.info("Throttled (%d) on %s; sleeping %.0fs "
                             "[refusal %d, %.0fs waited so far; %s]",
                             e.code, endpoint, delay, throttled,
                             throttled_for, self.limiter.describe())
                    if throttled == THROTTLE_HINT_AFTER:
                        log.warning(
                            "  still refused after %d attempts at the same "
                            "call. Both pools are reported above: if the user "
                            "pool has reset and calls are still refused, the "
                            "block is wider than this token -- the company "
                            "quota is shared with every other API consumer at "
                            "the site. Waits are capped at %ds and this will "
                            "keep retrying; nothing is lost by stopping "
                            "instead -- everything fetched is on disk and "
                            "re-running resumes.",
                            throttled, MAX_THROTTLE_SLEEP)
                    time.sleep(delay)
                    continue  # throttling is normal; retry indefinitely

                if e.code in TRANSIENT_STATUSES and attempt < self.max_transient_retries:
                    attempt += 1
                    delay = min(2**attempt, 60)
                    log.warning(
                        "Transient %d on %s (attempt %d/%d); retrying in %ds",
                        e.code, endpoint, attempt, self.max_transient_retries, delay,
                    )
                    time.sleep(delay)
                    continue

                self.stats["errors"] += 1
                return Response(e.code, headers, body, endpoint, params)

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.max_transient_retries:
                    attempt += 1
                    delay = min(2**attempt, 60)
                    log.warning("Network error on %s (%s); retrying in %ds", endpoint, e, delay)
                    time.sleep(delay)
                    continue
                self.stats["errors"] += 1
                return Response(0, {}, str(e).encode(), endpoint, params)


# -- endpoint helpers ----------------------------------------------------
# Thin named wrappers so call sites read as intent, and so every endpoint the
# project uses is enumerated in exactly one place.


def current_user(client: QuipClient):
    return client.get_json("/1/users/current")


def folders(client: QuipClient, folder_ids, include_chats=True):
    return client.get_json(
        "/1/folders/", ids=",".join(folder_ids), include_chats=str(bool(include_chats)).lower()
    )


def threads_v1(client: QuipClient, thread_ids):
    """V1 bulk: metadata + expanded_user_ids + is_deleted + live-app names in html."""
    return client.get_json("/1/threads/", ids=",".join(thread_ids))


def threads_v2(client: QuipClient, thread_ids):
    """V2 bulk: secret_path, spreadsheet_summary, sharing."""
    return client.get_json("/2/threads/", ids=",".join(thread_ids))


def thread_html_v2(client: QuipClient, thread_id) -> tuple[str, int, bool]:
    """Authoritative document content, reassembled across cursor pages.

    Returns `(html, pages, complete)`. `complete` is the important one: a
    partially-paginated document must never be recorded as fetched, or the
    export ships a prefix that reads as the whole thing.
    """
    pages, complete = client.paginate_all(f"/2/threads/{thread_id}/html")
    return "".join(p.get("html", "") for p in pages), len(pages), complete


def messages(client: QuipClient, thread_id, count=100, max_created_usec=None):
    return client.get_json(
        f"/1/messages/{thread_id}", count=count, max_created_usec=max_created_usec
    )


def blob(client: QuipClient, thread_id, blob_id) -> Response:
    return client.get(f"/1/blob/{thread_id}/{blob_id}")


def export_xlsx(client: QuipClient, thread_id) -> Response:
    """A real workbook for any thread carrying spreadsheet data.

    V1 only -- the V2 path returns 404. Threads with no embedded spreadsheet
    are rejected with 400, which is the documented behaviour and is why this is
    only called for threads with a `spreadsheet_summary`.
    """
    return client.get(f"/1/threads/{thread_id}/export/xlsx")


def users(client: QuipClient, user_ids):
    return client.get_json("/1/users/", ids=",".join(user_ids))
