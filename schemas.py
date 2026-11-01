"""JSON schemas for the ``stackexchange`` plugin tools."""

_SITE_PROP = {
    "type": "string",
    "description": (
        "Stack Exchange site: short name (`stackoverflow` default, `serverfault`, "
        "`superuser`, `askubuntu`, `unix`, `dba`, `security`, `codereview`, `math`, "
        "`softwareengineering`, ...) or a full domain (`stackoverflow.com`). "
        "Use se_sites to list valid values."
    ),
}

_FORMAT_PROP = {
    "type": "string",
    "enum": ["markdown", "json"],
    "description": "markdown (default) for a compact readable digest, json for raw API items.",
}

SE_SEARCH = {
    "name": "se_search",
    "description": (
        "Search Stack Exchange sites (Stack Overflow by default) for questions matching "
        "free text and/or tags. Use when you hit an error message, need an idiomatic "
        "solution, or want prior art for an API/behaviour question. Read-only. "
        "Returns question id, score, answer count, accepted flag, tags, link and, in "
        "excerpts mode, the matching text snippet from a question or answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free text matched against question titles and bodies. Best results "
                    "come from the distinctive part of the error or the exact API name "
                    "(e.g. `SQLSTATE[HY000] 1213 deadlock`, `Laravel queue timeout`). "
                    "Strip file paths, line numbers and variable names."
                ),
            },
            "title": {
                "type": "string",
                "description": "Restrict results to questions whose title contains this text.",
            },
            "tagged": {
                "type": "string",
                "description": "Semicolon-separated tags, at least one must be present (e.g. `laravel;queue`).",
            },
            "not_tagged": {
                "type": "string",
                "description": "Semicolon-separated tags that must NOT be present. Requires another criterion.",
            },
            "site": _SITE_PROP,
            "mode": {
                "type": "string",
                "enum": ["questions", "excerpts"],
                "description": (
                    "`questions` (default) returns question metadata only. `excerpts` also "
                    "returns the matched text snippet and can match answers — better when "
                    "you need to judge relevance from the body."
                ),
            },
            "sort": {
                "type": "string",
                "enum": ["relevance", "votes", "creation", "activity"],
                "description": "Result ordering. Default relevance.",
            },
            "accepted": {
                "type": "boolean",
                "description": "true: only questions with an accepted answer. false: only without. Omit for both. (questions mode)",
            },
            "min_answers": {
                "type": "integer",
                "description": "Minimum number of answers a question must have (questions mode).",
            },
            "limit": {
                "type": "integer",
                "description": "Results per page, 1-30. Default 8.",
            },
            "page": {
                "type": "integer",
                "description": "1-based page number, max 25. Default 1.",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
}

SE_QUESTION = {
    "name": "se_question",
    "description": (
        "Fetch one Stack Exchange question with its full body, its top answers (accepted "
        "answer first, then by votes) and top comments. Accepts a numeric question id, "
        "an answer id, or a pasted question/answer URL. Read-only. Use after se_search / "
        "se_similar to read the actual solution, or when the user pastes a Stack Overflow link."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Question id (`44715574`), answer id, or URL "
                    "(`https://stackoverflow.com/questions/44715574/...`, `.../a/44715862`)."
                ),
            },
            "site": _SITE_PROP,
            "answers_limit": {
                "type": "integer",
                "description": (
                    "How many answers to include, 0-15. Default 5. Set 0 for the question "
                    "only (no answers, no comments on answers). When > 0 the accepted answer "
                    "is always included, even if it ranks below the top-N by votes."
                ),
            },
            "answers_sort": {
                "type": "string",
                "enum": ["votes", "creation", "activity"],
                "description": "Answer ordering. Default votes.",
            },
            "comment_limit": {
                "type": "integer",
                "description": (
                    "Comments per post, 0-10. Default 3. Comments often contain the real "
                    "fix or a caveat, but they are noisy — set 0 to save tokens."
                ),
            },
            "max_body_chars": {
                "type": "integer",
                "description": "Truncate each post body to this many characters (0 = no limit). Default 6000.",
            },
            "format": _FORMAT_PROP,
        },
        "required": ["id"],
    },
}

SE_SIMILAR = {
    "name": "se_similar",
    "description": (
        "Find Stack Exchange questions similar to a title. Fastest path from a raw error "
        "message or a one-line problem statement to existing answers: the API does the "
        "semantic matching, so pass the error text or a natural title. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Question title or error message, e.g. `Laravel queue timeout exceeded`.",
            },
            "site": _SITE_PROP,
            "limit": {
                "type": "integer",
                "description": "Max results, 1-30. Default 8.",
            },
            "format": _FORMAT_PROP,
        },
        "required": ["title"],
    },
}

SE_SITES = {
    "name": "se_sites",
    "description": (
        "List Stack Exchange network sites with the exact `site` parameter value to pass to "
        "se_search / se_question / se_similar. Use when the question is not about general "
        "programming (e.g. server ops, databases, Ubuntu, security, math). Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Filter by name, slug or URL (e.g. `ubuntu`, `database`, `security`).",
            },
            "limit": {
                "type": "integer",
                "description": "Max sites to list, 1-100. Default 40.",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
}
