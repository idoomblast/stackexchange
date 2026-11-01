"""Stack Exchange API 2.3 client for the ``stackexchange`` Hermes plugin.

Read-only by construction: the plugin only ever sends the application ``key``
(which lifts the daily quota from 300 to 10,000 requests), never an OAuth
access token. No write method is reachable from this module.

Three behaviours matter for correctness:

* Custom response filters are created once per API key and cached on disk, so
  responses carry only the fields the tools render -- and ``body_markdown``
  instead of HTML. If filter creation fails the client degrades to the built-in
  ``withbody`` / ``default`` filters and converts HTML locally.
* Identical requests are cached for 60s. Stack Exchange explicitly asks that
  the same semantic request not be repeated faster than once a minute.
* ``backoff`` returned by the API is honoured by refusing the next call to that
  method with a retry hint instead of blocking the agent's turn for minutes.
"""

from __future__ import annotations

import gzip
import hashlib
import html as html_lib
import json
import logging
import os
import re
import threading
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.stackexchange.com/2.3"
DEFAULT_SITE = "stackoverflow"
USER_AGENT = "hermes-stackexchange-plugin/1.0 (+https://hermes-agent.nousresearch.com)"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

CACHE_TTL = 60.0
CACHE_MAX = 128
KEY_CACHE_TTL = 300.0

#: Query parameter the app key travels in. The docs also accept an
#: ``Authorization: Bearer <key>`` header; the query string is the form every
#: Stack Exchange wrapper has used for a decade and is what we probe with.
KEY_PARAM = "key"

_WRAPPER_FIELDS = [
    ".items", ".has_more", ".quota_max", ".quota_remaining",
    ".backoff", ".total", ".error_id", ".error_message", ".error_name",
]

_QUESTION_LIST_FIELDS = [
    "question.question_id", "question.title", "question.link", "question.score",
    "question.answer_count", "question.accepted_answer_id", "question.is_answered",
    "question.creation_date", "question.last_activity_date", "question.tags",
    "question.view_count",
]

_QUESTION_BODY_FIELDS = [
    "question.body_markdown",
]

_ANSWER_FIELDS = [
    "answer.answer_id", "answer.question_id", "answer.score", "answer.is_accepted",
    "answer.creation_date", "answer.last_activity_date", "answer.body_markdown",
]

_COMMENT_FIELDS = [
    "comment.comment_id", "comment.post_id", "comment.body", "comment.score",
    "comment.creation_date",
]

_EXCERPT_FIELDS = [
    "search_excerpt.question_id", "search_excerpt.answer_id",
    "search_excerpt.item_type", "search_excerpt.title", "search_excerpt.excerpt",
    "search_excerpt.score", "search_excerpt.is_accepted",
    "search_excerpt.has_accepted_answer", "search_excerpt.answer_count",
    "search_excerpt.tags", "search_excerpt.creation_date",
]

_SITE_FIELDS = [
    "site.api_site_parameter", "site.name", "site.site_url", "site.site_type",
    "site.audience", "site.site_state",
]

#: ``spec name -> included fields``. ``base=none`` means exactly these fields.
_FIELDS: dict[str, list[str]] = {
    "list": _WRAPPER_FIELDS + _QUESTION_LIST_FIELDS,
    "body": _WRAPPER_FIELDS + _QUESTION_LIST_FIELDS + _QUESTION_BODY_FIELDS + _ANSWER_FIELDS
    + _COMMENT_FIELDS,
    "excerpt": _WRAPPER_FIELDS + _EXCERPT_FIELDS,
    "site": _WRAPPER_FIELDS + _SITE_FIELDS,
}

#: Used when a custom filter cannot be created (missing/invalid key, API error).
_FALLBACK_FILTER = {
    "list": "default",
    "body": "withbody",
    "excerpt": "default",
    "site": "default",
}

_LOCK = threading.RLock()
_REQUEST_LOCK = threading.RLock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_backoff: dict[str, float] = {}
_filter_mem: dict[tuple[str, str], str] = {}
_key_cache: tuple[float, str | None] | None = None
_last_quota: dict[str, Any] = {}


class StackExchangeError(Exception):
    """An error reported by the API (or by the transport) in a tool-safe shape."""

    def __init__(
        self,
        name: str,
        message: str,
        *,
        error_id: int | None = None,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.message = message
        self.error_id = error_id
        self.status = status
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "error": scrub(self.name),
            "message": scrub(self.message),
        }
        # Never echo an API-supplied value verbatim: a malformed response could
        # put the credential in `error_id`. Only a genuine integer is meaningful,
        # and `int(400.9)`/`int("400")` must not launder something else into it.
        if isinstance(self.error_id, int) and not isinstance(self.error_id, bool):
            out["error_id"] = self.error_id
        if self.status is not None:
            out["http_status"] = int(self.status)
        if self.retry_after is not None:
            out["retry_after_seconds"] = int(self.retry_after)
        return out


_QUERY_SECRET_RE = re.compile(
    r"(\b(?:key|access_token|client_secret)=)[^&\s'\"<>]+", re.IGNORECASE
)


def scrub(text: str) -> str:
    """Strip the app key from anything that can reach a tool result or a log.

    ``requests`` stringifies a failed URL *including* query parameters
    (``Max retries exceeded with url: /2.3/search/advanced?...&key=...``), so a
    transport error would otherwise leak the credential into the agent's context
    and the gateway log.

    Must never raise: it is called from the error boundary itself, where a
    second failure would escape the tool's JSON contract.
    """
    if not text:
        return text
    try:
        text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
        # Whatever the configured key is, it must never survive these paths —
        # a short/malformed key is still a credential.
        key = get_api_key()
        if key:
            text = text.replace(key, "[REDACTED]")
    except Exception:  # noqa: BLE001 - a scrubber that throws is worse than one that misses
        return "[REDACTED: scrub failed]"
    return text


# --------------------------------------------------------------------------
# paths / config
# --------------------------------------------------------------------------

def hermes_home() -> Path:
    """Profile-aware Hermes home (``~/.hermes`` for the default profile)."""
    try:  # pragma: no cover - import path differs in standalone tests
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - Hermes internals absent in standalone runs
        env = os.environ.get("HERMES_HOME")
        return Path(env) if env else Path.home() / ".hermes"


def _config() -> dict[str, Any]:
    try:  # pragma: no cover - depends on Hermes internals
        from hermes_cli.config import load_config_readonly  # type: ignore

        data = load_config_readonly()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - config backend is optional
        return {}


def _env_file_value(path: Path, name: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def _read_key_file(path: Path) -> str | None:
    """Read a key file, never raising: an unreadable configured path must
    degrade to the unauthenticated quota instead of breaking the tool boundary."""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError as exc:
        logger.warning("stackexchange: cannot read key file %s (%s)",
                       path, type(exc).__name__)
        return None


def get_api_key() -> str | None:
    """Resolve the Stack Exchange app key.

    Order: env vars -> ``STACKEXCHANGE_API_KEY`` in ``<hermes>/.env`` ->
    ``stackexchange.api_key`` in config.yaml -> ``stackexchange.api_key_file``
    -> ``<hermes>/creds/stackexchange-api-key-lds``.

    A missing key is not fatal: the API still answers unauthenticated calls,
    just with the 300/day shared quota.
    """
    global _key_cache
    now = time.monotonic()
    with _LOCK:
        if _key_cache and now - _key_cache[0] < KEY_CACHE_TTL:
            return _key_cache[1]

    value: str | None = None
    candidate = os.environ.get("STACKEXCHANGE_API_KEY", "").strip()
    if candidate:
        value = candidate

    home = hermes_home()
    if not value:
        value = _env_file_value(home / ".env", "STACKEXCHANGE_API_KEY")

    section = (_config().get("stackexchange") or {}) if not value else {}
    if not value and isinstance(section, dict):
        raw = section.get("api_key")
        if isinstance(raw, str) and raw.strip():
            value = raw.strip()
        else:
            configured = section.get("api_key_file")
            if isinstance(configured, str) and configured.strip():
                value = _read_key_file(Path(os.path.expanduser(configured.strip())))

    if not value:
        value = _read_key_file(home / "creds" / "stackexchange-api-key-lds")

    with _LOCK:
        _key_cache = (now, value)
    return value


def invalidate_key_cache() -> None:
    global _key_cache
    with _LOCK:
        _key_cache = None


def normalize_site(site: str | None) -> str:
    """Normalise a ``site`` parameter (short name or full domain)."""
    value = (site or DEFAULT_SITE).strip().lower()
    value = re.sub(r"^https?://", "", value).rstrip("/")
    if value in {"so", "stack overflow", "stack-overflow"}:
        return "stackoverflow"
    return value or DEFAULT_SITE


_SITE_HOST_OVERRIDES = {
    "stackoverflow": "stackoverflow.com",
    "serverfault": "serverfault.com",
    "superuser": "superuser.com",
    "askubuntu": "askubuntu.com",
    "stackapps": "stackapps.com",
    "mathoverflow": "mathoverflow.net",
}


def site_base_url(site: str | None) -> str:
    """Best-effort site origin for links (question links come from the API)."""
    value = normalize_site(site)
    if "." in value:
        return f"https://{value}"
    host = _SITE_HOST_OVERRIDES.get(value) or f"{value}.stackexchange.com"
    return f"https://{host}"


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

def _filters_path() -> Path:
    return hermes_home() / "cache" / "stackexchange" / "filters.json"


def _load_filter_store() -> dict[str, dict[str, str]]:
    path = _filters_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    keys = raw.get("keys") if isinstance(raw, dict) else None
    if not isinstance(keys, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, spec_map in keys.items():
        if isinstance(spec_map, dict):
            out[str(key)] = {str(k): str(v) for k, v in spec_map.items() if isinstance(v, str)}
    return out


def _save_filter_store(store: dict[str, dict[str, str]]) -> None:
    path = _filters_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"v": 1, "keys": store}, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # cache is optional; never fail a tool call for it
        logger.debug("stackexchange: cannot persist filter cache: %s", exc)


def _filter_cache_key(key: str, spec: str) -> str:
    """Key the cache by API key *and* field list: editing ``_FIELDS`` must
    invalidate filters created by an older version of the plugin."""
    fields = ";".join(_FIELDS.get(spec) or [])
    return hashlib.sha256(f"{key}|{spec}|{fields}".encode()).hexdigest()[:16]


def invalidate_filter(spec: str, key: str | None) -> None:
    if not key:
        return
    digest = _filter_cache_key(key, spec)
    with _LOCK:
        _filter_mem.pop((digest, spec), None)
        store = _load_filter_store()
        if spec in store.get(digest, {}):
            store[digest].pop(spec, None)
            _save_filter_store(store)


def get_filter(spec: str, key: str | None) -> str | None:
    """Return a filter string for ``spec``, creating and caching it if needed."""
    fields = _FIELDS.get(spec)
    if not fields:
        return None
    if not key:
        return _FALLBACK_FILTER.get(spec)

    digest = _filter_cache_key(key, spec)
    with _LOCK:
        hit = _filter_mem.get((digest, spec))
    if hit:
        return hit

    store = _load_filter_store()
    cached = store.get(digest, {}).get(spec)
    if cached:
        with _LOCK:
            _filter_mem[(digest, spec)] = cached
        return cached

    try:
        data = api_get(
            "/filters/create",
            {"base": "none", "include": ";".join(fields), "unsafe": "false"},
            fields=None,
            use_cache=False,
        )
    except StackExchangeError as exc:
        logger.warning("stackexchange: filter create failed (%s); using %s",
                       scrub(exc.name), _FALLBACK_FILTER.get(spec))
        return _FALLBACK_FILTER.get(spec)

    items = data.get("items") or []
    created = items[0].get("filter") if items and isinstance(items[0], dict) else None
    if not isinstance(created, str) or not created:
        return _FALLBACK_FILTER.get(spec)

    with _LOCK:
        # Re-read under the lock and merge: a concurrent call for another spec
        # must not lose its entry to this write.
        store = _load_filter_store()
        store.setdefault(digest, {})[spec] = created
        _save_filter_store(store)
        _filter_mem[(digest, spec)] = created
    return created


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _looks_like_zlib(raw: bytes) -> bool:
    """A valid zlib header is two bytes whose big-endian value is a multiple of 31."""
    if len(raw) < 3 or raw[0] != 0x78:
        return False
    return ((raw[0] << 8) + raw[1]) % 31 == 0


def _looks_like_json(raw: bytes) -> bool:
    return raw.lstrip()[:1] in (b"{", b"[")


def _try_raw_deflate(raw: bytes) -> bytes | None:
    """Raw DEFLATE has no magic bytes; only accept a guess that yields JSON."""
    try:
        out = zlib.decompress(raw, -zlib.MAX_WBITS)
        json.loads(out.decode("utf-8", "replace"))
    except (zlib.error, ValueError):
        return None
    return out


def _decompress(raw: bytes, encoding: str | None) -> bytes:
    """The API always compresses; proxies occasionally strip the header."""
    if not raw:
        return raw
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    # Header-independent zlib detection: a stripped Content-Encoding must not
    # leave the caller holding compressed bytes it cannot parse.
    if _looks_like_zlib(raw):
        try:
            return zlib.decompress(raw)
        except zlib.error:
            pass
    if encoding and "deflate" in encoding.lower():
        try:  # HTTP "deflate" is ambiguous: some servers send raw DEFLATE
            return zlib.decompress(raw, -zlib.MAX_WBITS)
        except zlib.error:
            pass
    # A proxy that strips Content-Encoding also hides a raw-DEFLATE body, which
    # carries no magic bytes. Only guess when the bytes clearly are not text,
    # and only accept a guess that decompresses to a JSON document.
    if not _looks_like_json(raw):
        guess = _try_raw_deflate(raw)
        if guess is not None:
            return guess
    return raw


def _cache_get(cache_key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _LOCK:
        entry = _cache.get(cache_key)
        if not entry:
            return None
        stored_at, payload = entry
        if now - stored_at > CACHE_TTL:
            _cache.pop(cache_key, None)
            return None
        return payload


def _cache_put(cache_key: str, payload: dict[str, Any]) -> None:
    with _LOCK:
        if len(_cache) >= CACHE_MAX:
            oldest = min(_cache.items(), key=lambda kv: kv[1][0])[0]
            _cache.pop(oldest, None)
        _cache[cache_key] = (time.monotonic(), payload)


def clear_cache() -> None:
    with _LOCK:
        _cache.clear()
        _backoff.clear()


_ID_SEGMENT_RE = re.compile(r"\d+(?:;\d+)*$")


def _method_key(path: str) -> str:
    """Collapse ID segments so backoff applies to the API *method*.

    Stack Exchange backoff is a property of the method, not of one URL: a pause
    earned by ``/questions/123`` also covers ``/questions/456``.
    """
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return "/"
    # The id position of a parameterised route is canonicalised no matter what
    # the API returned there — a malformed id must not mint a new method key
    # and slip past a pause held for the real route.
    id_routes = {"questions", "answers"}
    for index, segment in enumerate(segments):
        if (index == 1 and segments[0] in id_routes) or _ID_SEGMENT_RE.match(segment):
            segments[index] = "{ids}"
    return "/" + "/".join(segments)


def _check_backoff(path: str) -> None:
    method = _method_key(path)
    now = time.monotonic()
    with _LOCK:
        until = _backoff.get(method)
    if until and now < until:
        remaining = round(until - now)
        raise StackExchangeError(
            "backoff",
            f"the API asked for a {remaining}s pause on {method}; "
            "try another method or retry later",
            retry_after=remaining,
        )


def _request(path: str, query: dict[str, str]) -> dict[str, Any]:
    url = API_BASE + path
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": USER_AGENT,
    }
    try:
        response = requests.get(
            url, params=query, headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            # A redirect is a credential-exfiltration path: the request carries
            # the app key, and `requests` would happily replay it to whatever
            # host the Location header names.
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise StackExchangeError(
            "network_error", scrub(f"{type(exc).__name__}: {exc}")
        ) from exc

    if 300 <= response.status_code < 400:
        raise StackExchangeError(
            "redirect_refused",
            f"the API answered with HTTP {response.status_code}; refusing to follow a "
            "redirect because the request carries the app key",
            status=response.status_code,
        )

    raw = _decompress(response.content, response.headers.get("Content-Encoding"))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        preview = raw[:200].decode("utf-8", "replace")
        raise StackExchangeError(
            "bad_response",
            f"non-JSON response (HTTP {response.status_code}): {scrub(preview)!r}",
            status=response.status_code,
        ) from exc

    if not isinstance(data, dict):
        raise StackExchangeError("bad_response", "unexpected response shape",
                                 status=response.status_code)

    if data.get("error_id"):
        raise StackExchangeError(
            str(data.get("error_name") or "api_error"),
            str(data.get("error_message") or "unknown API error"),
            error_id=data.get("error_id"),
            status=response.status_code,
        )

    try:
        backoff_seconds = int(data.get("backoff") or 0)
    except (TypeError, ValueError):
        backoff_seconds = 0
    if backoff_seconds > 0:
        method = _method_key(path)
        with _LOCK:
            _backoff[method] = time.monotonic() + backoff_seconds
        logger.info("stackexchange: %s requested backoff %ss", method, backoff_seconds)

    quota = {k: data.get(k) for k in ("quota_remaining", "quota_max", "has_more", "total")}
    with _LOCK:
        _last_quota.clear()
        _last_quota.update({k: v for k, v in quota.items() if v is not None})

    return data


def api_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    fields: str | None = "list",
    site: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """GET one API method. Raises :class:`StackExchangeError` on failure."""
    query: dict[str, str] = {}
    for name, value in (params or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        query[str(name)] = str(value)
    if site:
        query["site"] = normalize_site(site)

    app_key = get_api_key()
    cache_query = dict(query)
    if app_key:
        query[KEY_PARAM] = app_key

    # Serialise the check-then-request-then-store sequence. Without this, two
    # concurrent identical misses both call the API (burning quota) and can
    # create duplicate filters; with it, the second caller sees the first's
    # cache entry. RLock because get_filter() may recurse into api_get() for
    # /filters/create on the same thread.
    with _REQUEST_LOCK:
        last_error: StackExchangeError | None = None
        for attempt in (1, 2):
            # Checked under the lock: a concurrent request may have set backoff
            # while this caller was waiting for its turn.
            _check_backoff(path)
            call_query = dict(query)
            filt = get_filter(fields, app_key) if fields else None
            if filt:
                call_query["filter"] = filt

            cache_key = f"{path}?{urlencode(sorted(cache_query.items()))}|{filt or ''}"
            if use_cache:
                cached = _cache_get(cache_key)
                if cached is not None:
                    out = dict(cached)
                    out["_hermes"] = {"cached": True}
                    return out

            try:
                data = _request(path, call_query)
            except StackExchangeError as exc:
                last_error = exc
                retryable_filter = (
                    attempt == 1
                    and app_key
                    and exc.name == "bad_parameter"
                    and "filter" in exc.message.lower()
                )
                if retryable_filter:
                    invalidate_filter(fields, app_key)
                    continue
                raise

            if use_cache:
                _cache_put(cache_key, data)
            out = dict(data)
            out["_hermes"] = {"cached": False}
            return out

        raise last_error or StackExchangeError("api_error", "request failed")


def last_quota() -> dict[str, Any]:
    with _LOCK:
        return dict(_last_quota)


# --------------------------------------------------------------------------
# HTML -> text / markdown (fallback path + comments, which have no markdown)
# --------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "section", "article", "table", "tr", "blockquote",
               "ul", "ol", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}


class _BodyParser(HTMLParser):
    """Minimal HTML->Markdown converter for Stack Exchange post bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_pre = False
        self._href: str | None = None
        self._skip = 0
        self._list: list[str] = []
        self._quote = 0

    # -- helpers ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self._skip:
            return
        self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "pre":
            self._in_pre = True
            self._emit("\n\n```\n")
        elif tag == "code":
            if self._in_pre:
                lang = ""
                match = re.search(r"lang(?:uage)?-([\w+#.-]+)", attr.get("class", ""))
                if match:
                    lang = match.group(1)
                if lang and self.parts and self.parts[-1].endswith("```\n"):
                    self.parts[-1] = self.parts[-1][:-4] + f"```{lang}\n"
            else:
                self._emit("`")
        elif tag == "a":
            self._href = attr.get("href") or None
            self._emit("[")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag in ("ul", "ol"):
            self._list.append(tag)
            self._emit("\n")
        elif tag == "li":
            marker = "1. " if self._list and self._list[-1] == "ol" else "- "
            self._emit("\n" + marker)
        elif tag in _BLOCK_TAGS:
            if tag == "blockquote":
                self._quote += 1
                self._emit("\n\n> ")
            elif tag == "p" and self._quote:
                self._emit("\n> ")
            elif tag.startswith("h"):
                self._emit("\n\n### ")
            else:
                self._emit("\n\n")
        elif tag == "img":
            alt = attr.get("alt", "").strip()
            if alt:
                self._emit(f"[{alt}]")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "pre":
            self._in_pre = False
            self._emit("\n```\n\n")
        elif tag == "code":
            if not self._in_pre:
                self._emit("`")
        elif tag == "a":
            href = self._href
            self._href = None
            if href:
                self._emit(f"]({href})")
            elif self.parts and self.parts[-1] == "[":
                self.parts.pop()
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("ul", "ol"):
            if self._list:
                self._list.pop()
            self._emit("\n")
        elif tag in _BLOCK_TAGS:
            if tag == "blockquote":
                self._quote = max(0, self._quote - 1)
                self._emit("\n\n")
            elif tag == "p" and self._quote:
                pass  # the next <p> re-opens the "> " prefix
            else:
                self._emit("\n\n")

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def result(self) -> str:
        text = "".join(self.parts)
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(value: str | None) -> str:
    """Convert a Stack Exchange HTML body to Markdown (fallback when no filter)."""
    if not value:
        return ""
    parser = _BodyParser()
    try:
        parser.feed(value)
        parser.close()
        text = parser.result()
    except Exception:  # noqa: BLE001 - malformed HTML must never lose the payload
        text = html_to_text(value)
    return text


def html_to_text(value: str | None, *, single_line: bool = False) -> str:
    """Strip HTML from comment bodies and search excerpts."""
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote|pre)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    if single_line:
        text = re.sub(r"\s+", " ", text)
    else:
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# small formatting helpers shared by the tools
# --------------------------------------------------------------------------

def fmt_date(timestamp: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(timestamp)))
    except (TypeError, ValueError, OSError):
        return "?"


def fmt_count(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}m".replace(".0m", "m")
    if number >= 1_000:
        return f"{number / 1_000:.1f}k".replace(".0k", "k")
    return str(number)


def truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not limit or limit <= 0 or len(text) <= limit:
        return text
    return (
        text[:limit].rstrip()
        + f"\n\n… [truncated {len(text) - limit} chars; fetch the post URL for the rest]"
    )
