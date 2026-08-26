"""Offline tests for the matching layer.

The Cineplex API is not reachable from CI-less environments, so these run the
parser against a fixture shaped like the live response.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cineplex_watch as w  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "showtimes_sample.json").read_text())
CONFIG = json.loads((ROOT / "watch.config.json").read_text())


def run(cfg=None):
    return w.find_matches(FIXTURE, "9999", "Test Theatre", cfg or CONFIG)


class TestMatching(unittest.TestCase):
    def test_finds_exactly_the_bookable_imax70_sessions_on_the_target_date(self):
        keys = {h["key"].split(":")[-1] for h in run()}
        self.assertEqual(keys, {"HIT-A", "HIT-B", "HIT-C-TITLE-ONLY"})

    def test_falls_back_to_the_title_when_the_experience_lists_no_format(self):
        # Some records carry the format only in the film title.
        self.assertIn("HIT-C-TITLE-ONLY", [h["key"].split(":")[-1] for h in run()])

    def test_a_digital_session_under_a_70mm_title_is_still_excluded(self):
        # The regression that matters: "The Odyssey: The IMAX Experience in
        # 70MM Film" is the title of the *film*, not of every session under it.
        self.assertNotIn("WRONG-FORMAT", [h["key"].split(":")[-1] for h in run()])

    def test_excludes_other_dates(self):
        self.assertNotIn("WRONG-DATE-1", [h["key"].split(":")[-1] for h in run()])

    def test_excludes_non_imax70_formats(self):
        self.assertNotIn("WRONG-FORMAT", [h["key"].split(":")[-1] for h in run()])

    def test_excludes_other_films(self):
        self.assertNotIn("WRONG-MOVIE", [h["key"].split(":")[-1] for h in run()])

    def test_excludes_sessions_not_bookable_online(self):
        self.assertNotIn("NOT-BOOKABLE", [h["key"].split(":")[-1] for h in run()])

    def test_requireBookable_false_lets_unbookable_sessions_through(self):
        keys = {h["key"].split(":")[-1] for h in run(dict(CONFIG, requireBookable=False))}
        self.assertIn("NOT-BOOKABLE", keys)

    def test_any_format_mode_picks_up_the_digital_session(self):
        loose = dict(CONFIG, formatMatchAll=[], formatMatchAny=[])
        self.assertIn("WRONG-FORMAT", {h["key"].split(":")[-1] for h in run(loose)})

    def test_hit_carries_the_booking_link_and_seat_count(self):
        hit = next(h for h in run() if h["key"].endswith("HIT-B"))
        self.assertEqual(hit["url"], "https://www.cineplex.com/x/hit-b")
        self.assertEqual(hit["seatsRemaining"], 3)
        self.assertEqual(hit["date"], "2026-09-17")


class TestNormalisation(unittest.TestCase):
    def test_punctuation_and_spacing_are_ignored(self):
        for variant in ("70MM", "70 mm", "70-mm", "70mm Film"):
            self.assertTrue(w.matches_any(variant, ["70mm"]), variant)

    def test_matches_all_requires_every_token(self):
        self.assertTrue(w.matches_all("IMAX 70MM Film", ["imax", "70mm"]))
        self.assertFalse(w.matches_all("Digital IMAX", ["imax", "70mm"]))

    def test_flatten_text_handles_strings_dicts_and_lists(self):
        self.assertIn("imax", w.flatten_text(["IMAX", {"name": "70MM"}]))
        self.assertIn("70mm", w.flatten_text([{"experienceType": "70mm"}]))


class TestStateDedup(unittest.TestCase):
    def test_session_keys_are_stable_across_runs(self):
        self.assertEqual([h["key"] for h in run()], [h["key"] for h in run()])

    def test_key_is_namespaced_by_theatre(self):
        other = w.find_matches(FIXTURE, "1234", "Other", CONFIG)
        self.assertTrue(all(k["key"].startswith("1234:") for k in other))


class TestWebhookPayloads(unittest.TestCase):
    def _payload(self, url, monkey):
        captured = {}

        def fake_post(u, data, headers=None, timeout=20):
            captured["url"] = u
            captured["body"] = json.loads(data.decode())
            return 200

        monkey(fake_post)
        w.notify_webhook("Title", "line1\nline2", "https://example.com")
        return captured["body"]

    def setUp(self):
        self._real_post = w.http_post
        self._real_env = dict(w.os.environ)

    def tearDown(self):
        w.http_post = self._real_post
        w.os.environ.clear()
        w.os.environ.update(self._real_env)

    def test_slack_payload_uses_text(self):
        w.os.environ["WEBHOOK_URL"] = "https://hooks.slack.com/services/x"
        body = self._payload(None, lambda fn: setattr(w, "http_post", fn))
        self.assertIn("text", body)
        self.assertIn("line1", body["text"])

    def test_discord_payload_uses_content(self):
        w.os.environ["WEBHOOK_URL"] = "https://discord.com/api/webhooks/x"
        body = self._payload(None, lambda fn: setattr(w, "http_post", fn))
        self.assertIn("content", body)

    def test_no_webhook_configured_is_a_noop(self):
        w.os.environ.pop("WEBHOOK_URL", None)
        self.assertFalse(w.notify_webhook("t", "b", "u"))


class TestNtfyPayload(unittest.TestCase):
    def setUp(self):
        self._real_post = w.http_post
        self._real_env = dict(w.os.environ)
        self.sent = {}

        def fake_post(url, data, headers=None, timeout=20):
            self.sent = {"url": url, "body": json.loads(data.decode("utf-8")), "headers": headers}
            return 200

        w.http_post = fake_post
        w.os.environ["NTFY_TOPIC"] = "test-topic"

    def tearDown(self):
        w.http_post = self._real_post
        w.os.environ.clear()
        w.os.environ.update(self._real_env)

    def test_publishes_json_with_topic_title_and_click(self):
        self.assertTrue(w.notify_ntfy("Title", "body", "https://example.com/x"))
        body = self.sent["body"]
        self.assertEqual(body["topic"], "test-topic")
        self.assertEqual(body["title"], "Title")
        self.assertEqual(body["click"], "https://example.com/x")
        self.assertEqual(body["priority"], 5)

    def test_non_latin1_title_survives(self):
        # Regression: sending the title as an HTTP header raised
        # UnicodeEncodeError on the em dash, killing the alert silently.
        title, _body, _link = w.build_message(run(), CONFIG)
        with self.assertRaises(UnicodeEncodeError):
            title.encode("latin-1")  # this is what urllib does to headers
        self.assertTrue(w.notify_ntfy(title, "b", ""))
        self.assertEqual(self.sent["body"]["title"], title)

    def test_omits_click_when_there_is_no_link(self):
        w.notify_ntfy("t", "b", "")
        self.assertNotIn("click", self.sent["body"])

    def test_no_topic_configured_is_a_noop(self):
        w.os.environ.pop("NTFY_TOPIC", None)
        self.assertFalse(w.notify_ntfy("t", "b", "u"))


class TestApiKeyRecovery(unittest.TestCase):
    def setUp(self):
        self._real_get = w.http_get_json
        self._real_discover = w.discover_subscription_key

    def tearDown(self):
        w.http_get_json = self._real_get
        w.discover_subscription_key = self._real_discover

    def test_retries_once_with_a_fresh_key_after_a_401(self):
        calls = []

        def fake_get(url, headers=None, timeout=30):
            calls.append(headers["Ocp-Apim-Subscription-Key"])
            if len(calls) == 1:
                raise w.urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            return {"ok": True}

        w.http_get_json = fake_get
        w.discover_subscription_key = lambda *a, **k: "fresh"
        self.assertEqual(w.Api("stale").get("showtimes", {}), {"ok": True})
        self.assertEqual(calls, ["stale", "fresh"])

    def test_gives_up_after_a_second_401(self):
        def always_401(url, headers=None, timeout=30):
            raise w.urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        w.http_get_json = always_401
        w.discover_subscription_key = lambda *a, **k: "fresh"
        api = w.Api("stale")
        with self.assertRaises(w.urllib.error.HTTPError):
            api.get("showtimes", {})

    def test_other_http_errors_are_not_retried(self):
        calls = []

        def fake_get(url, headers=None, timeout=30):
            calls.append(1)
            raise w.urllib.error.HTTPError(url, 500, "Server Error", {}, None)

        w.http_get_json = fake_get
        with self.assertRaises(w.urllib.error.HTTPError):
            w.Api("k").get("showtimes", {})
        self.assertEqual(len(calls), 1)


class TestShapeTolerance(unittest.TestCase):
    """The live /theatres response did not match any of the keys originally
    guessed, and the watcher reported "0 theatres" instead of saying why.
    These cover the envelopes it now survives."""

    THEATRE = {"id": 1234, "name": "Cineplex Cinemas Vaughan"}

    def test_finds_a_bare_array(self):
        self.assertEqual(w.find_record_list([self.THEATRE], w.looks_like_theatre), [self.THEATRE])

    def test_finds_a_list_behind_an_unknown_envelope_key(self):
        for key in ("items", "theatres", "payload", "somethingNew"):
            found = w.find_record_list({key: [self.THEATRE]}, w.looks_like_theatre)
            self.assertEqual(found, [self.THEATRE], key)

    def test_finds_a_deeply_nested_list(self):
        blob = {"data": {"result": {"locations": [self.THEATRE]}}}
        self.assertEqual(w.find_record_list(blob, w.looks_like_theatre), [self.THEATRE])

    def test_returns_none_when_there_is_no_such_list(self):
        self.assertIsNone(w.find_record_list({"error": "nope"}, w.looks_like_theatre))
        self.assertIsNone(w.find_record_list({"dates": []}, w.looks_like_theatre))

    def test_a_record_needs_both_a_name_and_an_id(self):
        self.assertFalse(w.looks_like_theatre({"name": "No id"}))
        self.assertFalse(w.looks_like_theatre({"id": 1}))
        self.assertTrue(w.looks_like_theatre({"theatreId": 1, "theatreName": "X"}))

    def test_alternate_id_and_name_fields_are_accepted(self):
        blob = {"d": [{"locationId": "77", "displayName": "Cineplex Odeon"}]}
        self.assertEqual(len(w.find_record_list(blob, w.looks_like_theatre)), 1)

    def test_extract_days_prefers_the_top_level_dates_key(self):
        self.assertEqual(len(w.extract_days(FIXTURE)), 2)

    def test_extract_days_finds_a_wrapped_payload(self):
        self.assertEqual(len(w.extract_days({"result": {"dates": FIXTURE["dates"]}})), 2)

    def test_extract_days_on_a_useless_payload_is_empty_not_an_error(self):
        self.assertEqual(w.extract_days({"message": "unauthorized"}), [])

    def test_matching_still_works_through_a_wrapped_payload(self):
        wrapped = {"data": {"showtimes": {"dates": FIXTURE["dates"]}}}
        keys = {h["key"].split(":")[-1] for h in w.find_matches(wrapped, "9999", "T", CONFIG)}
        self.assertEqual(keys, {"HIT-A", "HIT-B", "HIT-C-TITLE-ONLY"})


class TestDescribeShape(unittest.TestCase):
    def test_summarises_without_dumping_everything(self):
        out = w.describe_shape({"dates": [{"startDate": "x", "movies": []}]})
        self.assertIn("dates", out)
        self.assertIn("1 x", out)

    def test_handles_scalars_and_empty_lists(self):
        self.assertEqual(w.describe_shape([]), "[0 x empty]")
        self.assertEqual(w.describe_shape("hi"), "str")


class TestEmptyBodyHandling(unittest.TestCase):
    """A date with nothing on it comes back as an empty body, not an empty
    array. That crashed the run with a JSONDecodeError -- fatal for a watcher
    whose normal state is 'nothing yet' for weeks."""

    class FakeResponse:
        def __init__(self, body, status=200, ctype="application/json"):
            self._body = body
            self.status = status
            self.headers = {"Content-Type": ctype}

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def setUp(self):
        self._real = w.urllib.request.urlopen

    def tearDown(self):
        w.urllib.request.urlopen = self._real

    def _serve(self, body, **kw):
        w.urllib.request.urlopen = lambda req, timeout=None: self.FakeResponse(body, **kw)
        return w.http_get_json("https://example.com/x")

    def test_empty_body_becomes_an_empty_dict(self):
        self.assertEqual(self._serve(b""), {})

    def test_whitespace_only_body_becomes_an_empty_dict(self):
        self.assertEqual(self._serve(b"   \n  "), {})

    def test_valid_json_still_parses(self):
        self.assertEqual(self._serve(b'{"dates": []}'), {"dates": []})

    def test_genuinely_broken_json_still_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            self._serve(b"<html>nope</html>")

    def test_an_empty_response_yields_no_matches_rather_than_an_error(self):
        self.assertEqual(w.find_matches({}, "7420", "T", CONFIG), [])
        self.assertEqual(w.extract_days({}), [])


class TestCompressedResponses(unittest.TestCase):
    """Cineplex gzips its showtimes responses and ignores Accept-Encoding:
    identity. Decoding the raw bytes as UTF-8 died on the gzip magic number."""

    import gzip as _gzip
    import zlib as _zlib

    class FakeResponse:
        def __init__(self, body, encoding=None, status=200):
            self._body = body
            self.status = status
            self.headers = {"Content-Type": "application/json"}
            if encoding:
                self.headers["Content-Encoding"] = encoding

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def setUp(self):
        self._real = w.urllib.request.urlopen

    def tearDown(self):
        w.urllib.request.urlopen = self._real

    def _serve(self, body, encoding=None):
        w.urllib.request.urlopen = lambda req, timeout=None: self.FakeResponse(body, encoding)
        return w.http_get_json("https://example.com/x")

    def test_gzip_body_with_a_content_encoding_header(self):
        packed = self._gzip.compress(b'{"dates": [{"movies": []}]}')
        self.assertEqual(self._serve(packed, "gzip"), {"dates": [{"movies": []}]})

    def test_gzip_body_with_no_header_is_sniffed_by_magic_bytes(self):
        # This is the real-world case: the header did not admit to gzip.
        packed = self._gzip.compress(b'{"ok": true}')
        self.assertEqual(self._serve(packed), {"ok": True})

    def test_deflate_body(self):
        self.assertEqual(self._serve(self._zlib.compress(b'{"ok": 1}'), "deflate"), {"ok": 1})

    def test_raw_deflate_without_a_zlib_header(self):
        comp = self._zlib.compressobj(wbits=-self._zlib.MAX_WBITS)
        packed = comp.compress(b'{"ok": 2}') + comp.flush()
        self.assertEqual(self._serve(packed, "deflate"), {"ok": 2})

    def test_uncompressed_body_is_untouched(self):
        self.assertEqual(self._serve(b'{"plain": true}'), {"plain": True})

    def test_empty_body_still_yields_an_empty_dict(self):
        self.assertEqual(self._serve(b""), {})

    def test_decompress_passes_through_when_there_is_nothing_to_do(self):
        self.assertEqual(w.decompress(b"hello", None), b"hello")
        self.assertEqual(w.decompress(b"", "gzip"), b"")

    def test_a_lying_gzip_header_on_an_uncompressed_body_is_not_fatal(self):
        # gzip.decompress() raises BadGzipFile here; the bytes win over the header.
        self.assertEqual(w.decompress(b"   ", "gzip"), b"   ")
        self.assertEqual(self._serve(b'{"ok": 3}', "gzip"), {"ok": 3})

    def test_a_lying_deflate_header_is_not_fatal(self):
        self.assertEqual(w.decompress(b"not deflate", "deflate"), b"not deflate")

    def test_an_empty_body_labelled_gzip_is_still_just_empty(self):
        self.assertEqual(self._serve(b"", "gzip"), {})

    def test_a_gzipped_showtimes_payload_survives_end_to_end(self):
        packed = self._gzip.compress(json.dumps(FIXTURE).encode())
        payload = self._serve(packed, "gzip")
        keys = {h["key"].split(":")[-1] for h in w.find_matches(payload, "7420", "T", CONFIG)}
        self.assertEqual(keys, {"HIT-A", "HIT-B", "HIT-C-TITLE-ONLY"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
