# stackexchange — Hermes plugin

Read-only Stack Exchange API v2.3 tools. Built for the "I hit an error, has
someone already solved this?" loop: search questions, read the accepted/top
answers, and cite the link.

No OAuth, no write access, no session state — only the application **key**,
which raises the daily quota from 300 to 10,000 requests.

## Tools

| Tool | What it does |
|---|---|
| `se_search` | Search questions by text/tags on any SE site. `mode="excerpts"` also returns the matching snippet (and matches answer bodies). |
| `se_question` | One question: body + top answers (accepted first) + top comments. Accepts a numeric id, an answer id, or a pasted URL. |
| `se_similar` | Questions similar to a title — the fastest path from a raw error message to existing answers. |
| `se_sites` | Network sites and their exact `site` parameter values. |

Every result ends with `_meta: quota N/M · has_more=…`, so the agent can see
when it is burning quota or when more pages exist.

## Configuration

All optional — the plugin degrades to the anonymous 300/day quota without a key.

| Source | Setting |
|---|---|
| Environment | `STACKEXCHANGE_API_KEY` |
| `~/.hermes/.env` | `STACKEXCHANGE_API_KEY=...` |
| `config.yaml` | `stackexchange.api_key`, or `stackexchange.api_key_file: /path/to/key` |
| Default file | `~/.hermes/creds/stackexchange-api-key-lds` |

Register the application at <https://stackapps.com/apps/oauth/register> to get
the key. The key is not a secret in the OAuth sense (it identifies the app, not
a user), but it is still stored in the creds directory and never logged.

## Behaviour

- **Filters** — custom response filters are created once per API key
  (`base=none`, only the fields the tools render, including `body_markdown`)
  and cached in `<hermes>/cache/stackexchange/filters.json`. If creation fails
  the client falls back to the built-in `withbody`/`default` filters and
  converts HTML to Markdown locally.
- **Cache** — identical requests are served from memory for 60 s. The API asks
  that the same request not be repeated faster than once a minute.
- **Backoff** — when the API returns `backoff`, the next call to that method
  fails fast with `retry_after_seconds` instead of blocking the turn.
- **Compression** — responses are gzip/deflate; a magic-number check handles
  proxies that strip `Content-Encoding`.

## Enable

```bash
# 1. add "stackexchange" under plugins.enabled in ~/.hermes/config.yaml
# 2. restart
hermes gateway restart
```

Toolset `stackexchange` is auto-enabled once the plugin loads; the bundled
skill is loaded with `skill_view("stackexchange:stackexchange-reference")`.

## Tests

```bash
/usr/bin/python3 tests/run_tests.py          # offline unit tests
/usr/bin/python3 tests/run_tests.py --live   # + live API smoke tests (uses the real key)
```

Use an interpreter that has `requests` (this host's bare `python3` may resolve
to one that does not; the harness then exits 2 with an explicit message).

The harness loads the plugin the same way the Hermes loader does (namespace
package + `spec_from_file_location`), so relative imports are exercised too.

## Licence / attribution

Stack Exchange content is CC BY-SA. Tool output always includes the canonical
link; cite it when reusing an answer.
