#!/usr/bin/env python3
"""Tests for the stackexchange plugin.

Run offline (unit) only:

    python3 tests/run_tests.py

Run unit + live API smoke tests (needs the key file / env var):

    python3 tests/run_tests.py --live

The module is loaded exactly the way the Hermes plugin loader does it
(namespace package + ``spec_from_file_location`` with ``submodule_search_locations``),
so this also proves the relative imports resolve in the gateway.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
import zlib
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
FAILURES: list[tuple[str, str]] = []

try:  # the plugin imports requests; fail with a clear message instead of a traceback
    import requests  # noqa: F401
except ModuleNotFoundError:
    print(
        "ERROR: this interpreter has no 'requests'. Run with one that does "
        "(/usr/bin/python3 or the Hermes runtime interpreter).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None


# --------------------------------------------------------------------------
# loader (mirrors hermes_cli/plugins.py)
# --------------------------------------------------------------------------

def load_plugin():
    package_name = "hermes_plugins"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = []  # namespace package
        sys.modules[package_name] = package

    module_name = f"{package_name}.stackexchange"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as exc:  # noqa: BLE001 - test harness
        FAILURES.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")


def eq(actual, expected, label=""):
    if actual != expected:
        raise AssertionError(f"{label} expected {expected!r}, got {actual!r}")


# --------------------------------------------------------------------------
# unit tests
# --------------------------------------------------------------------------

def unit_tests(plugin, client, tools, schemas):
    eq(client.normalize_site(None), "stackoverflow", "default site")
    eq(client.normalize_site("https://StackOverflow.com/"), "stackoverflow.com", "url site")
    eq(client.normalize_site("so"), "stackoverflow", "alias")
    eq(client.site_base_url("unix"), "https://unix.stackexchange.com", "subdomain site")
    eq(client.site_base_url("superuser"), "https://superuser.com", "override site")
    eq(client.site_base_url("askubuntu.com"), "https://askubuntu.com", "full domain")

    html = (
        "<p>Run the listener:</p>"
        "<pre><code class=\"lang-php\">php artisan queue:work --timeout=60\n</code></pre>"
        "<p>Then set <code>timeout</code> in <a href=\"https://laravel.com/docs/queues\">the docs</a>.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<blockquote><p>note</p></blockquote>"
    )
    md = client.html_to_markdown(html)
    assert "```php" in md, f"fence language missing: {md!r}"
    assert "php artisan queue:work --timeout=60" in md, "code content lost"
    assert "`timeout`" in md, "inline code lost"
    assert "[the docs](https://laravel.com/docs/queues)" in md, "link lost"
    assert "- one" in md and "- two" in md, "list lost"
    assert "> note" in md, f"blockquote lost: {md!r}"

    snippet = client.html_to_text(
        'I am creating <span class="highlight">Laravel</span> jobs &hellip; done',
        single_line=True,
    )
    eq(snippet, "I am creating Laravel jobs … done", "excerpt text")

    comment = client.html_to_text("<p>See <a href=\"#\">this</a>.</p><p>Second para</p>",
                                  single_line=True)
    eq(comment, "See this. Second para", "comment text")

    eq(client.fmt_count(999), "999", "count small")
    eq(client.fmt_count(1021), "1k", "count 1k")
    eq(client.fmt_count(15_300), "15.3k", "count 15k")

    truncated = client.truncate("x" * 100, 40)
    assert truncated.startswith("x" * 40) and "truncated 60 chars" in truncated, truncated
    eq(client.truncate("short", 40), "short", "no truncation")
    eq(client.truncate("short", 0), "short", "zero limit = unlimited")

    # -- question reference parsing
    eq(tools._parse_question_ref("44715574"), (44715574, "question"), "numeric id")
    eq(
        tools._parse_question_ref("https://stackoverflow.com/questions/44715574/laravel-queue"),
        (44715574, "question"),
        "question url",
    )
    eq(
        tools._parse_question_ref("https://stackoverflow.com/a/44715862"),
        (44715862, "answer"),
        "answer url",
    )
    try:
        tools._parse_question_ref("not-a-link")
    except ValueError:
        pass
    else:
        raise AssertionError("garbage id should raise ValueError")

    # -- filter cache round-trip inside a temp HERMES_HOME
    with tempfile.TemporaryDirectory() as tmp:
        original_home = client.hermes_home
        client.hermes_home = lambda: Path(tmp)
        try:
            store = {"abc": {"list": "!test"}}
            client._save_filter_store(store)
            eq(client._load_filter_store(), store, "filter store round-trip")
            eq(client._filters_path().parent, Path(tmp) / "cache" / "stackexchange",
               "filter cache location")
        finally:
            client.hermes_home = original_home

    # -- key resolution from a creds file
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "creds").mkdir()
        (home / "creds" / "stackexchange-api-key-lds").write_text("TESTKEY123\n")
        original_home, original_env = client.hermes_home, os.environ.pop(
            "STACKEXCHANGE_API_KEY", None)
        client.hermes_home = lambda: home
        client.invalidate_key_cache()
        try:
            eq(client.get_api_key(), "TESTKEY123", "key from creds file")
            os.environ["STACKEXCHANGE_API_KEY"] = "ENVKEY"
            client.invalidate_key_cache()
            eq(client.get_api_key(), "ENVKEY", "env wins over file")
            # An undocumented STACKEXCHANGE_KEY alias must not outrank .env.
            (home / ".env").write_text("STACKEXCHANGE_API_KEY=ENVFILEKEY\n")
            os.environ.pop("STACKEXCHANGE_API_KEY", None)
            os.environ["STACKEXCHANGE_KEY"] = "ALIASKEY"
            client.invalidate_key_cache()
            eq(client.get_api_key(), "ENVFILEKEY", "alias must not outrank .env")
        finally:
            os.environ.pop("STACKEXCHANGE_KEY", None)
            os.environ.pop("STACKEXCHANGE_API_KEY", None)
            if original_env is not None:
                os.environ["STACKEXCHANGE_API_KEY"] = original_env
            client.hermes_home = original_home
            client.invalidate_key_cache()

    # -- credential scrubbing: requests quotes the full URL (with key=) in errors
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "creds").mkdir()
        (home / "creds" / "stackexchange-api-key-lds").write_text("SECRETKEY123456\n")
        original_home = client.hermes_home
        client.hermes_home = lambda: home
        client.invalidate_key_cache()
        try:
            scrubbed = client.scrub(
                "Max retries exceeded with url: /2.3/search?key=SECRETKEY123456&site=so"
            )
            assert "SECRETKEY123456" not in scrubbed, scrubbed
            assert "key=[REDACTED]" in scrubbed, scrubbed
            eq(client.scrub("plain error"), "plain error", "scrub no-op")

            # A short configured key is still a credential: no length exemption.
            (home / "creds" / "stackexchange-api-key-lds").write_text("abcd\n")
            client.invalidate_key_cache()
            eq(client.scrub("prefix abcd suffix"), "prefix [REDACTED] suffix",
               "short key scrubbed literally")

            original_request = client._request

            def boom(path, query):
                raise client.StackExchangeError(
                    "network_error",
                    f"ConnectionError: url={path}?key={query.get('key')}&site=so",
                )

            client._request = boom
            client.clear_cache()
            try:
                out = tools.se_search({"query": "anything"})
            finally:
                client._request = original_request
                client.clear_cache()
            assert "SECRETKEY123456" not in out, f"key leaked into tool output: {out}"
            eq(json.loads(out)["error"], "network_error", "network error contract")
        finally:
            client.hermes_home = original_home
            client.invalidate_key_cache()

    # -- \u2014 round-2 gate fixes -------------------------------------------------
    # API-supplied error *names* must be scrubbed too, not only messages.
    leaky = client.StackExchangeError("KEY=SECRETKEY123456", "boom")
    assert "SECRETKEY123456" not in leaky.as_dict()["error"], leaky.as_dict()

    # scrub() must never raise, even when key resolution itself explodes.
    original_get_key = client.get_api_key

    def exploding_get_key():
        raise PermissionError("unreadable key file")

    client.get_api_key = exploding_get_key
    try:
        eq(client.scrub("anything"), "[REDACTED: scrub failed]", "scrub is non-throwing")
    finally:
        client.get_api_key = original_get_key

    # An unreadable key file must degrade to None, not raise.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "creds").mkdir()
        key_file = home / "creds" / "stackexchange-api-key-lds"
        key_file.write_text("UNREADABLEKEY\n")
        os.chmod(key_file, 0o000)
        original_home = client.hermes_home
        client.hermes_home = lambda: home
        client.invalidate_key_cache()
        try:
            if os.geteuid() != 0:  # root bypasses the permission bits
                eq(client.get_api_key(), None, "unreadable key file degrades to None")
        finally:
            os.chmod(key_file, 0o600)
            client.hermes_home = original_home
            client.invalidate_key_cache()

    # Deflate must decode even when a proxy strips Content-Encoding.
    payload = b'{"items": [], "has_more": false}'
    eq(client._decompress(zlib.compress(payload), None), payload, "zlib without header")
    eq(client._decompress(zlib.compress(payload), "deflate"), payload, "zlib with header")
    eq(client._decompress(gzip.compress(payload), None), payload, "gzip without header")
    eq(client._decompress(payload, None), payload, "plain body untouched")

    def raw_deflate(data: bytes) -> bytes:
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        return compressor.compress(data) + compressor.flush()

    eq(client._decompress(raw_deflate(payload), None), payload, "raw deflate without header")
    eq(client._decompress(raw_deflate(payload), "deflate"), payload, "raw deflate with header")
    garbage = raw_deflate(b"not json")
    eq(client._decompress(garbage, None), garbage,
       "a raw-deflate guess that is not JSON is discarded")
    brace_but_invalid = raw_deflate(b"{not json at all")
    eq(client._decompress(brace_but_invalid, None), brace_but_invalid,
       "a raw-deflate guess starting with '{' but not parsing is discarded")

    # Concurrent identical misses must coalesce into a single request.
    coalesced = {"n": 0}

    def slow_request(path, query):
        coalesced["n"] += 1
        time.sleep(0.2)
        return {"items": [{"question_id": 7}], "has_more": False,
                "quota_remaining": 1, "quota_max": 1}

    original_request = client._request
    client._request = slow_request
    client.clear_cache()
    results: list = []
    try:
        threads = [threading.Thread(
            target=lambda: results.append(client.api_get("/coalesce", {"q": "x"}, fields=None))
        ) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        client._request = original_request
        client.clear_cache()
    eq(coalesced["n"], 1, "concurrent identical calls coalesced")
    eq(len(results), 2, "both callers got a result")

    # answers_limit=0 is a documented opt-out: question only, no answer calls.
    seen_paths: list[str] = []

    def fake_api_get(path, params=None, **kwargs):
        seen_paths.append(path)
        if path == "/questions/123":
            return {"items": [{
                "question_id": 123, "title": "T", "accepted_answer_id": 999,
                "answer_count": 1, "tags": ["x"], "link": "https://so/questions/123",
                "score": 1, "view_count": 1, "creation_date": 1,
                "last_activity_date": 1, "body_markdown": "question body",
            }], "quota_remaining": 1, "quota_max": 1}
        raise AssertionError(f"unexpected call {path}")

    original_api_get = tools.api_get
    tools.api_get = fake_api_get
    try:
        out = tools.se_question({"id": "123", "answers_limit": 0, "comment_limit": 0})
    finally:
        tools.api_get = original_api_get
    assert "## Question" in out and "## Answers" not in out, out[:300]
    eq(seen_paths, ["/questions/123"], "no answer/comment calls when answers_limit=0")

    # -- round-3 gate fixes -------------------------------------------------
    # An API-supplied non-integer error_id must be dropped, not echoed.
    dropped = client.StackExchangeError("boom", "msg", error_id="KEY=SECRETKEY123456")
    assert "error_id" not in dropped.as_dict(), dropped.as_dict()
    eq(client.StackExchangeError("boom", "msg", error_id=400).as_dict()["error_id"], 400,
       "integer error_id preserved")
    for bad in (400.9, "400", True, [400]):
        assert "error_id" not in client.StackExchangeError(
            "boom", "msg", error_id=bad).as_dict(), f"error_id laundered {bad!r}"

    # Redirects must be refused (a redirect could replay the key to another host).
    class FakeResponse:
        def __init__(self):
            self.status_code = 302
            self.headers: dict = {}
            self.content = b""

    class FakeRequests:
        @staticmethod
        def get(*args, **kwargs):
            assert kwargs.get("allow_redirects") is False, "redirects must be disabled"
            return FakeResponse()

    original_requests = client.requests
    client.requests = FakeRequests
    try:
        try:
            client.api_get("/redirect", {"q": "x"}, fields=None)
        except client.StackExchangeError as exc:
            eq(exc.name, "redirect_refused", "redirect error name")
        else:
            raise AssertionError("redirect was followed")
    finally:
        client.requests = original_requests
        client.clear_cache()

    # Backoff set by an in-flight request must be seen by the next waiter.
    backoff_calls: list = []
    backoff_errors: list = []

    def backoff_request(path, query):
        backoff_calls.append(query.get("q"))
        time.sleep(0.2)
        client._backoff[path] = time.monotonic() + 60
        return {"items": [], "has_more": False}

    def call(path, value):
        try:
            client.api_get(path, {"q": value}, fields=None)
        except client.StackExchangeError as exc:
            backoff_errors.append(exc.name)

    client._request = backoff_request
    client.clear_cache()
    try:
        first = threading.Thread(target=call, args=("/backoff-race", "a"))
        second = threading.Thread(target=call, args=("/backoff-race", "b"))
        first.start()
        time.sleep(0.05)
        second.start()
        first.join()
        second.join()
    finally:
        client._request = original_request
        client.clear_cache()
    eq(len(backoff_calls), 1, "waiter must not issue a request after backoff was set")
    eq(backoff_errors, ["backoff"], "waiter sees backoff set by the in-flight request")

    # answers_limit=0 with an /a/<id> URL must be rejected before any network call.
    seen_again: list = []

    def never_called(path, params=None, **kwargs):
        seen_again.append(path)
        raise AssertionError(f"unexpected call {path}")

    tools.api_get = never_called
    try:
        out = tools.se_question({"id": "https://stackoverflow.com/a/44715862",
                                 "answers_limit": 0})
    finally:
        tools.api_get = original_api_get
    eq(json.loads(out)["error"], "bad_request", "answer URL + limit 0 rejected")
    eq(seen_again, [], "no network call for answer URL + limit 0")

    # Accepted answer outside the top-N window must still be included, first.
    def accepted_api_get(path, params=None, **kwargs):
        if path == "/questions/321":
            return {"items": [{
                "question_id": 321, "title": "Q", "accepted_answer_id": 9999,
                "answer_count": 3, "tags": [], "link": "https://so/questions/321",
                "score": 1, "view_count": 1, "creation_date": 1,
                "last_activity_date": 1, "body_markdown": "question body",
            }], "quota_remaining": 1, "quota_max": 1}
        if path == "/questions/321/answers":
            return {"items": [
                {"answer_id": 1, "question_id": 321, "score": 10, "is_accepted": False,
                 "creation_date": 1, "last_activity_date": 1, "body_markdown": "top"},
                {"answer_id": 2, "question_id": 321, "score": 5, "is_accepted": False,
                 "creation_date": 1, "last_activity_date": 1, "body_markdown": "second"},
            ], "quota_remaining": 1, "quota_max": 1}
        if path == "/answers/9999":
            return {"items": [{
                "answer_id": 9999, "question_id": 321, "score": 1, "is_accepted": True,
                "creation_date": 1, "last_activity_date": 1,
                "body_markdown": "accepted",
            }], "quota_remaining": 1, "quota_max": 1}
        raise AssertionError(f"unexpected call {path}")

    tools.api_get = accepted_api_get
    try:
        out = tools.se_question({"id": "321", "answers_limit": 2, "comment_limit": 0,
                                 "max_body_chars": 0})
    finally:
        tools.api_get = original_api_get
    assert "aid 9999" in out and "✔ accepted" in out, out[:400]
    assert out.index("aid 9999") < out.index("aid 1"), "accepted answer must be first"

    # Accepted answer already inside the top-N but ranked below a higher-voted
    # answer must still be rendered first, without a second /answers/{id} call.
    reordered_calls: list = []

    def reorder_api_get(path, params=None, **kwargs):
        reordered_calls.append(path)
        if path == "/questions/654":
            return {"items": [{
                "question_id": 654, "title": "Q2", "accepted_answer_id": 77,
                "answer_count": 2, "tags": [], "link": "https://so/questions/654",
                "score": 1, "view_count": 1, "creation_date": 1,
                "last_activity_date": 1, "body_markdown": "body",
            }], "quota_remaining": 1, "quota_max": 1}
        if path == "/questions/654/answers":
            return {"items": [
                {"answer_id": 11, "question_id": 654, "score": 40, "is_accepted": False,
                 "creation_date": 1, "last_activity_date": 1, "body_markdown": "top"},
                {"answer_id": 77, "question_id": 654, "score": 9, "is_accepted": True,
                 "creation_date": 1, "last_activity_date": 1, "body_markdown": "accepted"},
            ], "quota_remaining": 1, "quota_max": 1}
        raise AssertionError(f"unexpected call {path}")

    tools.api_get = reorder_api_get
    try:
        out = tools.se_question({"id": "654", "answers_limit": 2, "comment_limit": 0,
                                 "max_body_chars": 0})
    finally:
        tools.api_get = original_api_get
    assert out.index("aid 77") < out.index("aid 11"), "already-present accepted answer not moved"
    assert "/answers/77" not in reordered_calls, "accepted answer refetched unnecessarily"
    eq(out.count("aid 77"), 1, "accepted answer duplicated")

    # se_sites JSON must expose quota like the other JSON handlers.
    def sites_api_get(path, params=None, **kwargs):
        return {"items": [{"api_site_parameter": "stackoverflow",
                           "name": "Stack Overflow", "site_type": "main_site",
                           "audience": "professional and enthusiast programmers"}],
                "has_more": False, "quota_remaining": 4321, "quota_max": 10000}

    tools.api_get = sites_api_get
    try:
        payload = json.loads(tools.se_sites({"query": "stack", "format": "json"}))
    finally:
        tools.api_get = original_api_get
    eq(payload["quota"], {"remaining": 4321, "max": 10000}, "se_sites json quota")

    # -- the output boundary scrubs success and error payloads alike
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "creds").mkdir()
        (home / "creds" / "stackexchange-api-key-lds").write_text("LEAKKEY1234\n")
        original_home = client.hermes_home
        client.hermes_home = lambda: home
        client.invalidate_key_cache()
        client.clear_cache()

        def leaky_api_get(path, params=None, **kwargs):
            return {
                "items": [{
                    "question_id": 1, "title": "how to fix LEAKKEY1234",
                    "link": "https://stackoverflow.com/q/1?key=LEAKKEY1234",
                    "score": 1, "answer_count": 0, "is_answered": True,
                    "tags": ["LEAKKEY1234"], "creation_date": 1,
                    "last_activity_date": 1, "owner": {"display_name": "LEAKKEY1234"},
                }],
                "quota_remaining": 1, "quota_max": 1, "has_more": False,
            }

        tools.api_get = leaky_api_get
        try:
            out_md = tools.se_search({"query": "x"})
            out_json = tools.se_search({"query": "x", "format": "json"})
            out_err = tools.se_question({"id": "LEAKKEY1234"})
        finally:
            tools.api_get = original_api_get
            client.hermes_home = original_home
            client.invalidate_key_cache()
            client.clear_cache()
        assert "LEAKKEY1234" not in out_md, out_md[:300]
        assert "LEAKKEY1234" not in out_json, out_json[:300]
        assert "LEAKKEY1234" not in out_err, out_err[:300]

    # -- a key colliding with JSON syntax must not corrupt the document
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "creds").mkdir()
        (home / "creds" / "stackexchange-api-key-lds").write_text("false\n")
        original_home = client.hermes_home
        client.hermes_home = lambda: home
        client.invalidate_key_cache()
        client.clear_cache()

        def falsey_api_get(path, params=None, **kwargs):
            return {"items": [{
                "question_id": 2, "title": "false positives everywhere",
                "link": "https://stackoverflow.com/q/2", "score": 3,
                "answer_count": 1, "is_answered": True, "tags": ["false"],
                "creation_date": 1, "last_activity_date": 1,
            }], "quota_remaining": 1, "quota_max": 1, "has_more": False}

        tools.api_get = falsey_api_get
        try:
            err_json = tools.se_search({})                     # local validation error
            ok_json = tools.se_search({"query": "x", "format": "json"})
            ok_md = tools.se_search({"query": "x"})
        finally:
            tools.api_get = original_api_get
            client.hermes_home = original_home
            client.invalidate_key_cache()
            client.clear_cache()

        eq(json.loads(err_json)["success"], False,
           "error JSON still parses when the key is the literal 'false'")
        eq(json.loads(ok_json)["success"], True,
           "success JSON still parses when the key is the literal 'false'")
        assert "false positives" not in ok_json and "false positives" not in ok_md, ok_json[:200]

    # Backoff is method-level: a pause earned by one id covers the whole method.
    class BackoffResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.headers: dict = {}
            self.content = json.dumps(payload).encode("utf-8")

    class BackoffRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None, allow_redirects=None):
            return BackoffResponse({"items": [], "backoff": 30,
                                    "quota_remaining": 1, "quota_max": 1})

    original_requests = client.requests
    client.requests = BackoffRequests
    client.clear_cache()
    try:
        client._request("/questions/123", {})          # real transport path stores it
        eq(sorted(client._backoff), ["/questions/{ids}"],

           "backoff stored against the method, not the concrete path")
        try:
            client.api_get("/questions/456", {}, fields=None)
        except client.StackExchangeError as exc:
            eq(exc.name, "backoff", "same method, different id blocked")
        else:
            raise AssertionError("backoff did not apply to the same method")

        try:
            client.api_get("/questions/1;2;3", {}, fields=None)
        except client.StackExchangeError as exc:
            eq(exc.name, "backoff", "id-list of the same method blocked")
        else:
            raise AssertionError("backoff missed a semicolon id list")

        eq(client.api_get("/similar", {"title": "x"}, fields=None)["items"], [],
           "another method stays usable through api_get")
    finally:
        client.requests = original_requests
        client.clear_cache()

    # A malformed id must not mint a new method key and slip past a pause.
    for raw, expected in [
        ("/questions/123x", "/questions/{ids}"),
        ("/questions/abc/answers", "/questions/{ids}/answers"),
        ("/answers/zzz/comments", "/answers/{ids}/comments"),
        ("/answers/1;2;x", "/answers/{ids}"),
    ]:
        eq(client._method_key(raw), expected, f"method key for {raw}")

    client.clear_cache()
    client._backoff["/questions/{ids}"] = time.monotonic() + 30
    try:
        try:
            client.api_get("/questions/123x", {}, fields=None)
        except client.StackExchangeError as exc:
            eq(exc.name, "backoff", "malformed id still meets the method pause")
        else:
            raise AssertionError("malformed id bypassed the method backoff")
    finally:
        client.clear_cache()

    # A malformed id coming back from the API is a clean error, not a request.
    bogus_calls: list = []

    def bogus_api_get(path, params=None, **kwargs):
        bogus_calls.append(path)
        if path == "/answers/999":
            return {"items": [{"answer_id": 999, "question_id": "123x",
                               "score": 1, "is_accepted": False,
                               "creation_date": 1, "last_activity_date": 1,
                               "body_markdown": "seed"}],
                    "quota_remaining": 1, "quota_max": 1}
        raise AssertionError(f"unexpected call {path}")

    tools.api_get = bogus_api_get
    try:
        out = tools.se_question({"id": "https://stackoverflow.com/a/999"})
    finally:
        tools.api_get = original_api_get
    eq(json.loads(out)["error"], "bad_response", "malformed question_id rejected")
    eq(bogus_calls, ["/answers/999"], "no request built from a malformed id")

    # A *present* accepted_answer_id must be a real id — 0 and False are not.
    def question_fixture(accepted_marker):
        def fake(path, params=None, **kwargs):
            if path == "/questions/777":
                item = {"question_id": 777, "title": "Q", "answer_count": 0,
                        "tags": [], "link": "https://so/q/777", "score": 1,
                        "view_count": 1, "creation_date": 1,
                        "last_activity_date": 1, "body_markdown": "body"}
                if accepted_marker is not ...:
                    item["accepted_answer_id"] = accepted_marker
                return {"items": [item], "quota_remaining": 1, "quota_max": 1}
            if path == "/questions/777/answers":
                return {"items": [], "quota_remaining": 1, "quota_max": 1}
            raise AssertionError(f"unexpected call {path}")
        return fake

    for bad_accepted in (0, False):
        tools.api_get = question_fixture(bad_accepted)
        try:
            out = tools.se_question({"id": "777", "answers_limit": 1})
        finally:
            tools.api_get = original_api_get
        eq(json.loads(out)["error"], "bad_response",
           f"accepted_answer_id={bad_accepted!r} rejected")

    tools.api_get = question_fixture(...)          # field genuinely absent
    try:
        out = tools.se_question({"id": "777", "answers_limit": 1, "comment_limit": 0})
    finally:
        tools.api_get = original_api_get
    assert "## Question" in out and '"success": false' not in out, out[:200]

    # A malformed answer_id must be rejected *before* the comments fan-out.
    comment_calls: list = []

    def malformed_answer_fixture(path, params=None, **kwargs):
        comment_calls.append(path)
        if path == "/questions/888":
            return {"items": [{"question_id": 888, "title": "Q", "answer_count": 1,
                               "tags": [], "link": "https://so/q/888", "score": 1,
                               "view_count": 1, "creation_date": 1,
                               "last_activity_date": 1, "body_markdown": "body"}],
                    "quota_remaining": 1, "quota_max": 1}
        if path == "/questions/888/answers":
            return {"items": [{"answer_id": "9x", "question_id": 888, "score": 1,
                               "is_accepted": False, "creation_date": 1,
                               "last_activity_date": 1, "body_markdown": "a"}],
                    "quota_remaining": 1, "quota_max": 1}
        raise AssertionError(f"unexpected call {path}")

    tools.api_get = malformed_answer_fixture
    try:
        out = tools.se_question({"id": "888", "answers_limit": 2, "comment_limit": 2})
    finally:
        tools.api_get = original_api_get
    eq(json.loads(out)["error"], "bad_response", "malformed answer_id rejected")
    assert "/questions/888/comments" not in comment_calls, comment_calls

    # -- cache and backoff without any network
    original_request = client._request
    calls = {"n": 0}

    def fake_request(path, query):
        calls["n"] += 1
        return {"items": [{"question_id": 1}], "has_more": False,
                "quota_remaining": 9999, "quota_max": 10000}

    client._request = fake_request
    client.clear_cache()
    try:
        first = client.api_get("/fake", {"q": "x"}, fields=None)
        second = client.api_get("/fake", {"q": "x"}, fields=None)
        eq(calls["n"], 1, "second identical call served from cache")
        eq(first["items"], second["items"], "cached payload equality")
        eq(second["_hermes"]["cached"], True, "cache hit marker")

        client.clear_cache()
        client._backoff["/fake"] = __import__("time").monotonic() + 60
        try:
            client.api_get("/fake", {"q": "y"}, fields=None)
        except client.StackExchangeError as exc:
            eq(exc.name, "backoff", "backoff error name")
            assert exc.retry_after and exc.retry_after > 50, exc.retry_after
        else:
            raise AssertionError("backoff was not enforced")
    finally:
        client._request = original_request
        client.clear_cache()

    # -- registration contract
    registered = {}

    class FakeCtx:
        def register_tool(self, name, toolset, schema, handler, **kwargs):
            registered[name] = (toolset, schema, handler)

        def register_skill(self, name, path):
            registered[f"skill:{name}"] = path

    plugin.register(FakeCtx())
    eq(
        sorted(k for k in registered if not k.startswith("skill:")),
        ["se_question", "se_search", "se_similar", "se_sites"],
        "registered tools",
    )
    eq(registered["se_search"][0], "stackexchange", "toolset name")
    assert registered["skill:stackexchange-reference"].exists(), "bundled skill missing"
    for name in ("se_search", "se_question", "se_similar", "se_sites"):
        schema = getattr(schemas, f"SE_{name.split('_', 1)[1].upper()}")
        eq(registered[name][1], schema, f"{name} schema identity")


# --------------------------------------------------------------------------
# live smoke tests
# --------------------------------------------------------------------------

def live_tests(tools):
    def search():
        out = tools.se_search({"query": "Laravel queue timeout exceeded", "limit": 3})
        assert "qid" in out and "score" in out, out[:400]
        assert '"success": false' not in out, out[:400]

    def search_excerpts():
        out = tools.se_search({"query": "SQLSTATE HY000 1213 deadlock", "mode": "excerpts",
                               "limit": 2})
        assert "qid" in out and ">" in out, out[:400]

    def similar():
        out = tools.se_similar({"title": "Laravel queue timeout exceeded", "limit": 3})
        assert "qid" in out, out[:400]

    def question():
        out = tools.se_question({"id": "44715574", "answers_limit": 2,
                                 "comment_limit": 2, "max_body_chars": 800})
        assert "## Question" in out and "## Answers" in out, out[:600]

    def question_from_answer_url():
        out = tools.se_question({"id": "https://stackoverflow.com/a/44715862",
                                 "answers_limit": 1, "max_body_chars": 400})
        assert "## Question" in out, out[:400]

    def sites():
        out = tools.se_sites({"query": "ubuntu", "limit": 5})
        assert "`askubuntu`" in out, out[:400]

    def bad_site():
        out = tools.se_search({"query": "x", "site": "definitely-not-a-site"})
        payload = json.loads(out)
        assert payload["error"] == "bad_parameter", payload

    for name, fn in [
        ("live se_search", search),
        ("live se_search excerpts", search_excerpts),
        ("live se_similar", similar),
        ("live se_question", question),
        ("live se_question from answer url", question_from_answer_url),
        ("live se_sites", sites),
        ("live bad site -> error contract", bad_site),
    ]:
        check(name, fn)


def main() -> int:
    plugin = load_plugin()
    sys.path.insert(0, str(PLUGIN_DIR))
    from hermes_plugins.stackexchange import client, schemas, tools

    print("== unit ==")
    unit_tests(plugin, client, tools, schemas)

    if "--live" in sys.argv:
        print("\n== live ==")
        if not client.get_api_key():
            print("SKIP live: no API key resolved")
        else:
            live_tests(tools)
            print(f"\nquota after live run: {client.last_quota()}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for name, message in FAILURES:
            print(f"  - {name}: {message}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
