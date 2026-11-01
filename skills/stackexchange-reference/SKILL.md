---
name: stackexchange-reference
description: Use when looking up Stack Overflow / Stack Exchange answers while debugging. Search strategies, tool parameters, and how to read the results without burning quota.
version: 1.0.0
author: idoom
license: MIT
metadata:
  hermes:
    tags: [stackoverflow, stackexchange, debugging, reference]
    related_skills: [systematic-debugging, web-flow-recording]
---

# Stack Exchange reference lookup

Four read-only tools over the official Stack Exchange API v2.3. They are faster
and cheaper than `web_search` when the question is "has someone already solved
this exact error", because the API returns structured questions, scores and
accepted-answer flags instead of HTML pages.

## Which tool first

| Situation | Tool |
|---|---|
| Raw error message / stack trace line | `se_similar` (title = the error text) |
| Concept or API question ("how does X work in Y") | `se_search` |
| Need the matching text, not just titles | `se_search` with `mode="excerpts"` |
| Non-programming topic (nginx, MySQL, Ubuntu, security) | `se_sites` then retarget |
| User pasted an SO link, or you have a question id | `se_question` |

## Search strategy that actually works

1. **Strip the noise before searching.** File paths, line numbers, memory
   addresses, request ids, timestamps and variable names are unique to the
   user's machine and match nothing. Keep the library name, the exception
   class, and the stable phrase: `SQLSTATE[HY000] 1213 deadlock`, not
   `app/Services/OrderService.php:184 SQLSTATE[HY000] 1213`.
2. **Tag beats free text** for a known stack. `tagged="laravel;queue"` with a
   short `query` outperforms a long sentence.
3. **Two passes.** First `sort="relevance"` to see what exists. Then, if the
   topic has many duplicates, `sort="votes"` (with `accepted=true`) to surface
   the canonical answer instead of the newest me-too question.
4. **`mode="excerpts"` when titles are ambiguous** — the snippet comes from
   question *and* answer bodies, so you can judge relevance without a second
   call. Excerpts do not carry `link`; build it from the returned `qid`.
5. **`se_similar` before `se_search` for errors.** It matches semantically on
   the title, which is exactly how SO's own "similar questions" box works.

## Reading the results

- `score` on the **question** measures how many people found it interesting,
  not whether it is solved. `✔ accepted` is the signal that an answer worked
  for the asker; a high-vote unaccepted answer often works better.
- Always open the question (`se_question`) before quoting an answer: the top
  answer may be obsolete (old syntax, deprecated API) and the correction lives
  in **comments**. Comments are included by default for this reason.
- `creation_date` / `last_activity_date` matter more than score for fast-moving
  ecosystems (Laravel, Next.js, Cloudflare, Docker). A 2017 answer with 400
  votes can be worse than a 2025 answer with 4.
- Answers are CC BY-SA. When you reuse content in a written response, cite the
  question/answer URL.

## Cost, quota and rate limits

- App key raises the daily quota to **10,000**; without a key it is **300**
  shared per IP. `_meta: quota N/M` appears at the end of every tool result —
  check it if a call fails unexpectedly.
- The plugin caches identical requests for **60 s**; Stack Exchange treats
  faster repeats of the same request as abusive.
- If the API returns `backoff`, the tool refuses that method and reports
  `retry_after_seconds` instead of blocking. Use a different method
  (`se_similar` vs `se_search`) or continue with other work.
- `has_more=true` means more pages exist; raise `page` (max 25) rather than
  `limit` (max 30 per page).

## Failure modes

| Error | Meaning / action |
|---|---|
| `bad_request` | No criterion passed — the API requires `query`, `title` or `tagged`. |
| `bad_parameter` | Invalid site name — run `se_sites` for the exact slug. |
| `not_found` | Question id does not exist on that site (ids are per-site). |
| `backoff` | Honour `retry_after_seconds`; do not retry the same method immediately. |
| `network_error` | Transport failure; retry once, then fall back to `web_search`. |
