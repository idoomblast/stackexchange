"""Tool handlers for the ``stackexchange`` plugin.

All four tools are read-only wrappers over the Stack Exchange API 2.3. Output is
compact Markdown by default (token-cheap for the agent); ``format="json"``
returns the raw API items for callers that need to post-process.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Callable
from typing import Any

from . import client
from .client import (
    DEFAULT_SITE,
    StackExchangeError,
    api_get,
    fmt_count,
    fmt_date,
    html_to_markdown,
    html_to_text,
    normalize_site,
    site_base_url,
    truncate,
)

logger = logging.getLogger(__name__)

_SORTS = {"relevance", "votes", "creation", "activity"}


# --------------------------------------------------------------------------
# tiny helpers
# --------------------------------------------------------------------------

def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class _Json(str):
    """Marker for a payload that is already value-scrubbed JSON."""


def _scrub_values(value: Any) -> Any:
    """Scrub string *values* recursively; JSON syntax is never rewritten.

    Scrubbing the serialised text instead would corrupt the document whenever
    the configured key happens to equal a JSON literal (``false``), a number or
    a property name.
    """
    if isinstance(value, str):
        return client.scrub(value)
    if isinstance(value, dict):
        return {key: _scrub_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_values(item) for item in value]
    return value


def _dump(payload: Any, **kwargs: Any) -> str:
    kwargs.setdefault("ensure_ascii", False)
    return _Json(json.dumps(_scrub_values(payload), **kwargs))


def _api_id(value: Any, label: str) -> int:
    """An id taken from an API response must be a positive int before it is
    interpolated into a request path."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StackExchangeError("bad_response", f"the API returned a malformed {label}")
    return value


def _guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Convert API and unexpected exceptions into the tool JSON error contract."""

    def wrapper(args: dict | None = None, **kwargs: Any) -> str:
        try:
            result = fn(args or {}, **kwargs)
            # JSON from `_dump` is already value-scrubbed; Markdown is text.
            return result if isinstance(result, _Json) else client.scrub(result)
        except StackExchangeError as exc:
            try:
                payload: dict[str, Any] = {"success": False, **exc.as_dict()}
                quota = client.last_quota()
                if quota:
                    payload["quota"] = quota
                return _dump(payload)
            except Exception:  # noqa: BLE001 - last line of defence for the JSON contract
                return _dump({"success": False, "error": "api_error",
                              "message": "Stack Exchange request failed"})
        except Exception as exc:  # noqa: BLE001 - tool boundary must never leak a traceback
            try:
                # Scrub before logging: a requests/urllib3 traceback quotes the
                # full URL, query string (and therefore the API key).
                logger.error("stackexchange: tool %s failed:\n%s",
                             fn.__name__, client.scrub(traceback.format_exc()))
                message = client.scrub(f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - the boundary itself must not fail
                message = "internal error"
            return _dump({"success": False, "error": "internal_error",
                          "message": message})

    wrapper.__name__ = getattr(fn, "__name__", "handler")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _error(kind: str, message: str, **extra: Any) -> str:
    return _dump({"success": False, "error": kind, "message": message, **extra})


def _meta_line(data: dict[str, Any], extra: list[str] | None = None) -> str:
    bits = list(extra or [])
    quota_remaining = data.get("quota_remaining")
    quota_max = data.get("quota_max")
    if quota_remaining is not None:
        bits.append(f"quota {quota_remaining}/{quota_max}" if quota_max else f"quota {quota_remaining}")
    if data.get("has_more"):
        bits.append("has_more=true")
    if (data.get("_hermes") or {}).get("cached"):
        bits.append("cached<60s")
    return f"_meta: {' · '.join(bits)}_" if bits else ""


def _answer_link(question_link: str | None, answer_id: Any, site: str) -> str:
    if question_link and "/questions/" in question_link:
        return question_link.split("/questions/")[0] + f"/a/{answer_id}"
    return f"{site_base_url(site)}/a/{answer_id}"


def _question_line(index: int, item: dict[str, Any], site: str) -> str:
    question_id = item.get("question_id")
    title = item.get("title") or "(untitled)"
    link = item.get("link") or f"{site_base_url(site)}/questions/{question_id}"
    if item.get("accepted_answer_id"):
        status = "✔ accepted"
    elif item.get("is_answered"):
        status = "answered"
    else:
        status = "unanswered"
    tags = ", ".join(item.get("tags") or []) or "-"
    return "\n".join([
        f"{index}. **{title}**",
        (
            f"   qid {question_id} · score {item.get('score', 0)} · "
            f"{item.get('answer_count', 0)} answers · {status} · "
            f"{fmt_count(item.get('view_count'))} views · "
            f"asked {fmt_date(item.get('creation_date'))}"
        ),
        f"   tags: {tags}",
        f"   {link}",
    ])


def _excerpt_line(index: int, item: dict[str, Any], site: str) -> str:
    question_id = item.get("question_id")
    answer_id = item.get("answer_id")
    kind = item.get("item_type") or "question"
    title = item.get("title") or "(untitled)"
    snippet = html_to_text(item.get("excerpt"), single_line=True)
    if len(snippet) > 420:
        snippet = snippet[:420].rstrip() + "…"
    if kind == "answer" and answer_id:
        link = f"{site_base_url(site)}/a/{answer_id}"
    else:
        link = f"{site_base_url(site)}/questions/{question_id}"
    tags = ", ".join(item.get("tags") or [])
    head = f"{index}. **{title}**" + (" — *answer*" if kind == "answer" else "")
    meta = f"   qid {question_id}"
    if answer_id:
        meta += f" · aid {answer_id}"
    meta += f" · score {item.get('score', 0)}"
    if tags:
        meta += f" · tags: {tags}"
    return "\n".join([head, meta, f"   > {snippet}", f"   {link}"])


# --------------------------------------------------------------------------
# se_search
# --------------------------------------------------------------------------

@_guard
def se_search(args: dict[str, Any], **kwargs: Any) -> str:
    query = _str(args.get("query"))
    title = _str(args.get("title"))
    tagged = _str(args.get("tagged"))
    not_tagged = _str(args.get("not_tagged"))
    site = normalize_site(args.get("site") or DEFAULT_SITE)
    mode = (_str(args.get("mode")) or "questions").lower()
    sort = (_str(args.get("sort")) or "relevance").lower()
    output = (_str(args.get("format")) or "markdown").lower()
    limit = _clamp_int(args.get("limit"), 1, 30, 8)
    page = _clamp_int(args.get("page"), 1, 25, 1)
    accepted = args.get("accepted")
    min_answers = args.get("min_answers")

    if sort not in _SORTS:
        sort = "relevance"
    if mode not in {"questions", "excerpts"}:
        mode = "questions"

    if not (query or title or tagged):
        return _error("bad_request",
                      "Provide at least one of: query, title, tagged "
                      "(the API rejects a search with no criteria).")

    if mode == "excerpts":
        params: dict[str, Any] = {
            "q": query or title, "tagged": tagged, "nottagged": not_tagged,
            "sort": sort, "order": "desc", "pagesize": limit, "page": page,
        }
        data = api_get("/search/excerpts", params, fields="excerpt", site=site)
        items = data.get("items") or []
        if output == "json":
            return _dump(
                {"success": True, "site": site, "mode": mode, "items": items,
                 "quota": {"remaining": data.get("quota_remaining"),
                           "max": data.get("quota_max")},
                 "has_more": data.get("has_more")},
                ensure_ascii=False, indent=2,
            )
        header = f"# Stack Exchange — {site} · excerpts for `{query or title or tagged or not_tagged}`"
        lines = [header, ""]
        if not items:
            lines.append("_No matching question or answer excerpts._")
        for offset, item in enumerate(items):
            lines.append(_excerpt_line((page - 1) * limit + offset + 1, item, site))
            lines.append("")
        lines.append(_meta_line(data))
        return "\n".join(lines).strip()

    params = {
        "q": query, "title": title, "tagged": tagged, "nottagged": not_tagged,
        "sort": sort, "order": "desc", "pagesize": limit, "page": page,
    }
    if isinstance(accepted, bool):
        params["accepted"] = accepted
    if min_answers is not None:
        params["answers"] = _clamp_int(min_answers, 0, 100, 0)

    data = api_get("/search/advanced", params, fields="list", site=site)
    items = data.get("items") or []

    if output == "json":
        return _dump(
            {"success": True, "site": site, "mode": mode, "items": items,
             "quota": {"remaining": data.get("quota_remaining"),
                       "max": data.get("quota_max")},
             "total": data.get("total"), "has_more": data.get("has_more")},
            ensure_ascii=False, indent=2,
        )

    criteria = query or title or tagged
    lines = [f"# Stack Exchange — {site} · search `{criteria}`"]
    filters_used = []
    if tagged:
        filters_used.append(f"tagged={tagged}")
    if not_tagged:
        filters_used.append(f"not_tagged={not_tagged}")
    if isinstance(accepted, bool):
        filters_used.append(f"accepted={str(accepted).lower()}")
    if min_answers is not None:
        filters_used.append(f"min_answers={params.get('answers')}")
    lines.append(f"sort={sort} · page {page} · limit {limit}"
                 + (f" · {' · '.join(filters_used)}" if filters_used else ""))
    lines.append("")
    if not items:
        lines.append("_No questions matched._")
        hints = []
        if isinstance(accepted, bool):
            hints.append("drop `accepted`")
        if min_answers is not None:
            hints.append("lower `min_answers`")
        if not_tagged:
            hints.append("drop `not_tagged`")
        if hints:
            lines.append(f"_Hint: {' / '.join(hints)}, or use mode=\"excerpts\" to match answer bodies too._")
        else:
            lines.append("_Hint: fewer terms, the exact exception class, or mode=\"excerpts\"._")
    for offset, item in enumerate(items):
        lines.append(_question_line((page - 1) * limit + offset + 1, item, site))
        lines.append("")
    lines.append(_meta_line(data))
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# se_question
# --------------------------------------------------------------------------

def _parse_question_ref(raw: Any) -> tuple[int, str]:
    """Accept 12345, /questions/12345/slug, /q/12345, /a/67890."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("'id' is required (question id or question/answer URL)")
    match = re.search(r"/questions/(\d+)", text)
    if match:
        return int(match.group(1)), "question"
    match = re.search(r"/a/(\d+)", text)
    if match:
        return int(match.group(1)), "answer"
    match = re.fullmatch(r"(\d+)", text)
    if match:
        return int(match.group(1)), "question"
    raise ValueError(f"cannot parse a question id from {text!r}")


def _render_comments(comments: list[dict[str, Any]]) -> list[str]:
    lines = ["**Comments**"]
    for comment in comments:
        text = html_to_text(comment.get("body"), single_line=True)
        if len(text) > 320:
            text = text[:320].rstrip() + "…"
        lines.append(f"- ({comment.get('score', 0)}) {text}")
    return lines


@_guard
def se_question(args: dict[str, Any], **kwargs: Any) -> str:
    site = normalize_site(args.get("site") or DEFAULT_SITE)
    output = (_str(args.get("format")) or "markdown").lower()
    answers_limit = _clamp_int(args.get("answers_limit"), 0, 15, 5)
    comment_limit = _clamp_int(args.get("comment_limit"), 0, 10, 3)
    answers_sort = (_str(args.get("answers_sort")) or "votes").lower()
    if answers_sort not in {"votes", "creation", "activity"}:
        answers_sort = "votes"
    max_body_chars = _clamp_int(args.get("max_body_chars"), 0, 100_000, 6000)

    try:
        reference, kind = _parse_question_ref(args.get("id"))
    except ValueError as exc:
        return _error("bad_request", str(exc))

    if kind == "answer" and answers_limit == 0:
        return _error(
            "bad_request",
            "an answer URL needs answers_limit > 0 — the id is resolved through "
            "/answers/{id}, which is an answer call. Pass the question id for "
            "question-only mode.",
        )

    if kind == "answer":
        resolved = api_get(f"/answers/{reference}", {}, fields="body", site=site)
        answers = resolved.get("items") or []
        if not answers:
            return _error("not_found", f"answer {reference} not found on {site}")
        question_id = _api_id(answers[0].get("question_id"), "question_id")
        seed_answer = answers[0]
    else:
        question_id, seed_answer = reference, None

    question_data = api_get(f"/questions/{question_id}", {}, fields="body", site=site)
    questions = question_data.get("items") or []
    if not questions:
        return _error("not_found", f"question {question_id} not found on {site}")
    question = questions[0]

    answer_items: list[dict[str, Any]] = []
    answer_ids: list[int] = []
    if answers_limit > 0:
        answer_data = api_get(
            f"/questions/{question_id}/answers",
            {"sort": answers_sort, "order": "desc", "pagesize": answers_limit},
            fields="body",
            site=site,
        )
        answer_items = answer_data.get("items") or []
        accepted_raw = question.get("accepted_answer_id")
        # Absent (None) is legitimate — an unanswered question. Any value that
        # is present must be a real id, including 0/False which are not.
        accepted_id = (None if accepted_raw is None
                       else _api_id(accepted_raw, "accepted_answer_id"))
        if accepted_id:
            position = next(
                (i for i, a in enumerate(answer_items) if a.get("answer_id") == accepted_id),
                None,
            )
            if position is None:
                extra = api_get(f"/answers/{accepted_id}", {}, fields="body", site=site)
                if extra.get("items"):
                    answer_items.insert(0, extra["items"][0])
            elif position > 0:  # present but outranked by votes: still show it first
                answer_items.insert(0, answer_items.pop(position))
        if seed_answer and all(a.get("answer_id") != seed_answer.get("answer_id")
                               for a in answer_items):
            answer_items.append(seed_answer)
        # Validate before the comments fan-out: a malformed id must not spend
        # a /questions/{id}/comments request first.
        answer_ids = [_api_id(a.get("answer_id"), "answer_id") for a in answer_items]

    comments: dict[int, list[dict[str, Any]]] = {}
    if comment_limit > 0:
        question_comments = api_get(
            f"/questions/{question_id}/comments",
            {"sort": "votes", "order": "desc", "pagesize": comment_limit * 4},
            fields="body", site=site,
        ).get("items") or []
        comments[question_id] = question_comments[:comment_limit]

        if answer_ids:
            ids = ";".join(str(i) for i in answer_ids)
            answer_comments = api_get(
                f"/answers/{ids}/comments",
                {"sort": "votes", "order": "desc",
                 "pagesize": min(100, comment_limit * len(answer_items) * 4)},
                fields="body", site=site,
            ).get("items") or []
            grouped: dict[int, list[dict[str, Any]]] = {}
            for comment in answer_comments:
                grouped.setdefault(comment.get("post_id"), []).append(comment)
            for post_id, items in grouped.items():
                comments[post_id] = items[:comment_limit]

    if output == "json":
        return _dump(
            {"success": True, "site": site, "question": question,
             "answers": answer_items, "comments": comments,
             "quota": {"remaining": question_data.get("quota_remaining"),
                       "max": question_data.get("quota_max")}},
            ensure_ascii=False, indent=2,
        )

    question_body = question.get("body_markdown") or html_to_markdown(question.get("body"))
    lines: list[str] = [
        f"# {question.get('title') or '(untitled)'}",
        "",
        (
            f"qid {question.get('question_id')} · score {question.get('score', 0)} · "
            f"{question.get('answer_count', 0)} answers · "
            f"{fmt_count(question.get('view_count'))} views · "
            f"asked {fmt_date(question.get('creation_date'))} · "
            f"last activity {fmt_date(question.get('last_activity_date'))}"
        ),
        f"tags: {', '.join(question.get('tags') or []) or '-'}",
        question.get("link") or "",
        "",
        "## Question",
        "",
        truncate(question_body, max_body_chars),
        "",
    ]

    if comments.get(question_id):
        lines.extend(_render_comments(comments[question_id]))
        lines.append("")

    if answer_items:
        lines.append(f"## Answers (top {len(answer_items)} by {answers_sort})")
        lines.append("")
        for answer in answer_items:
            answer_id = answer.get("answer_id")
            accepted = bool(answer.get("is_accepted")) or answer_id == question.get("accepted_answer_id")
            heading = "### ✔ accepted" if accepted else "### answer"
            lines.append(
                f"{heading} · score {answer.get('score', 0)} · aid {answer_id} · "
                f"{fmt_date(answer.get('creation_date'))}"
            )
            lines.append(_answer_link(question.get("link"), answer_id, site))
            lines.append("")
            body = answer.get("body_markdown") or html_to_markdown(answer.get("body"))
            lines.append(truncate(body, max_body_chars))
            lines.append("")
            if comments.get(answer_id):
                lines.extend(_render_comments(comments[answer_id]))
                lines.append("")
    elif answers_limit > 0:
        lines.extend(["_No answers._", ""])

    lines.append(_meta_line(question_data))
    lines.append("_Source: Stack Exchange (CC BY-SA) — cite the link above._")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# se_similar
# --------------------------------------------------------------------------

@_guard
def se_similar(args: dict[str, Any], **kwargs: Any) -> str:
    title = _str(args.get("title"))
    site = normalize_site(args.get("site") or DEFAULT_SITE)
    output = (_str(args.get("format")) or "markdown").lower()
    limit = _clamp_int(args.get("limit"), 1, 30, 8)

    if not title:
        return _error("bad_request", "'title' is required — pass the question title "
                                     "or the error message you are chasing.")

    data = api_get(
        "/similar",
        {"title": title, "sort": "relevance", "order": "desc", "pagesize": limit},
        fields="list", site=site,
    )
    items = data.get("items") or []

    if output == "json":
        return _dump(
            {"success": True, "site": site, "items": items,
             "quota": {"remaining": data.get("quota_remaining"),
                       "max": data.get("quota_max")},
             "has_more": data.get("has_more")},
            ensure_ascii=False, indent=2,
        )

    lines = [f"# Stack Exchange — {site} · similar to `{title}`", ""]
    if not items:
        lines.append("_No similar questions found. Try se_search with the error text._")
    for index, item in enumerate(items, 1):
        lines.append(_question_line(index, item, site))
        lines.append("")
    lines.append(_meta_line(data))
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# se_sites
# --------------------------------------------------------------------------

@_guard
def se_sites(args: dict[str, Any], **kwargs: Any) -> str:
    query = _str(args.get("query")).lower()
    output = (_str(args.get("format")) or "markdown").lower()
    limit = _clamp_int(args.get("limit"), 1, 100, 40)

    items: list[dict[str, Any]] = []
    first = api_get("/sites", {"pagesize": 100, "page": 1}, fields="site")
    items.extend(first.get("items") or [])
    if first.get("has_more"):
        second = api_get("/sites", {"pagesize": 100, "page": 2}, fields="site")
        items.extend(second.get("items") or [])

    if query:
        items = [
            item for item in items
            if query in str(item.get("api_site_parameter", "")).lower()
            or query in str(item.get("name", "")).lower()
            or query in str(item.get("site_url", "")).lower()
        ]
    items.sort(key=lambda item: str(item.get("name", "")).lower())
    items = items[:limit]

    if output == "json":
        return _dump(
            {"success": True, "items": items,
             "quota": {"remaining": first.get("quota_remaining"),
                       "max": first.get("quota_max")}},
            ensure_ascii=False, indent=2,
        )

    lines = [f"# Stack Exchange network sites ({len(items)} shown)", "",
             "Pass the `site` parameter as the value in backticks.", ""]
    for item in items:
        lines.append(
            f"- `{item.get('api_site_parameter')}` — {item.get('name')} "
            f"({item.get('site_type') or '?'}, {item.get('audience') or '?'})"
        )
    lines.append("")
    meta_source = dict(first)
    meta_source["has_more"] = False  # both pages were already fetched above
    lines.append(_meta_line(meta_source))
    return "\n".join(lines).strip()
