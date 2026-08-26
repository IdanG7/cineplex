"""Offline tests for the matching layer.

The Cineplex API is not reachable from CI-less environments, so these run the
parser against a fixture shaped like the live response.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_a_top_level_array_response_is_handled(self):
        # Confirmed live: theatre 7408 answers /showtimes with
        # [{"theatre": ..., "theatreId": ..., "dates": [...]}] rather than an object.
        live = [{"theatre": "Cineplex Cinemas Vaughan", "theatreId": 7408, "dates": FIXTURE["dates"]}]
        self.assertEqual(len(w.extract_days(live)), 2)
        keys = {h["key"].split(":")[-1] for h in w.find_matches(live, "7408", "Vaughan", CONFIG)}
        self.assertEqual(keys, {"HIT-A", "HIT-B", "HIT-C-TITLE-ONLY"})

    def test_a_204_empty_response_is_simply_no_showtimes(self):
        # Confirmed live: theatre 7420 answers HTTP 204 with no body for a date
        # that has nothing on it. That is the watcher's resting state.
        self.assertEqual(w.find_matches({}, "7420", "Mississauga", CONFIG), [])

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


def auditorium(rows=15, cols=20, aisle_after=None):
    """A synthetic IMAX house: `rows` rows labelled A.., `cols` seats each."""
    out = []
    for r in range(rows):
        label = chr(ord("A") + r)
        seats = []
        for c in range(1, cols + 1):
            column = c if not aisle_after or c <= aisle_after else c + 3
            seats.append({"id": f"{label}{c}", "label": str(c), "column": column, "type": "standard"})
        out.append({"label": label, "seats": seats})
    return {"standardSeats": {"rows": out}}


SEAT_CFG = {"seats": {"partySize": 5, "targetRowFraction": 0.65, "rowWeight": 1.0,
                      "centerWeight": 0.8, "avoidSeatTypes": ["wheelchair", "companion"]}}


class TestSeatRanking(unittest.TestCase):
    def rank(self, layout=None, avail=None, cfg=None):
        return w.rank_seat_blocks(layout or auditorium(), avail or {}, cfg or SEAT_CFG)

    def test_best_block_sits_about_two_thirds_back(self):
        best = self.rank()[0]
        # 15 rows, target 0.65 -> row J is the closest row centre.
        self.assertEqual(best["row"], "J")
        self.assertAlmostEqual(best["rowFraction"], 9.5 / 15, places=3)

    def test_best_block_is_centred_within_half_a_seat(self):
        best = self.rank()[0]
        cols = [int(s) for s in best["seats"]]
        self.assertEqual(len(cols), 5)
        # Row spans 1..20, centre 10.5; five seats cannot straddle it exactly.
        self.assertLessEqual(abs((cols[0] + cols[-1]) / 2 - 10.5), 0.5)

    def test_it_returns_five_adjacent_seats(self):
        for block in self.rank()[:20]:
            cols = [int(s) for s in block["seats"]]
            self.assertEqual(cols, list(range(cols[0], cols[0] + 5)))

    def test_a_block_never_straddles_an_aisle(self):
        layout = auditorium(aisle_after=10)
        for block in w.rank_seat_blocks(layout, {}, SEAT_CFG):
            labels = [int(s) for s in block["seats"]]
            self.assertFalse(min(labels) <= 10 < max(labels), f"straddles aisle: {labels}")

    def test_taken_seats_are_excluded(self):
        # Block out the centre of row J; the best block must move.
        avail = {"seatAvailabilities": {f"J{c}": "Sold" for c in range(7, 15)}}
        best = self.rank(avail=avail)[0]
        if best["row"] == "J":
            self.assertFalse(set(range(7, 15)) & {int(s) for s in best["seats"]})

    def test_accessible_seats_are_not_recommended(self):
        layout = auditorium()
        for seat in layout["standardSeats"]["rows"][9]["seats"][7:13]:
            seat["type"] = "Wheelchair"
        best = self.rank(layout=layout)[0]
        if best["row"] == "J":
            self.assertFalse(set(range(8, 14)) & {int(s) for s in best["seats"]})

    def test_front_and_back_rows_rank_worse_than_the_middle(self):
        ranked = self.rank()
        by_row = {}
        for block in ranked:
            by_row.setdefault(block["row"], block["score"])
        self.assertLess(by_row["J"], by_row["A"])
        self.assertLess(by_row["J"], by_row["O"])

    def test_a_party_too_large_for_the_row_yields_nothing(self):
        cfg = {"seats": dict(SEAT_CFG["seats"], partySize=40)}
        self.assertEqual(w.rank_seat_blocks(auditorium(), {}, cfg), [])

    def test_an_unreadable_layout_yields_nothing_rather_than_raising(self):
        self.assertEqual(w.rank_seat_blocks({}, {}, SEAT_CFG), [])
        self.assertEqual(w.rank_seat_blocks({"nope": 1}, {}, SEAT_CFG), [])


class TestSeatHelpers(unittest.TestCase):
    def test_showtime_id_from_the_obvious_field(self):
        self.assertEqual(w.session_showtime_id({"vistaSessionId": "388367"}), "388367")

    def test_showtime_id_falls_back_to_the_seat_map_url(self):
        # Confirmed live: seatMapUrl carries ?theatreId=7420&showtimeId=388367
        session = {"seatMapUrl": "https://www.cineplex.com/x?theatreId=7420&showtimeId=388367"}
        self.assertEqual(w.session_showtime_id(session), "388367")

    def test_showtime_id_falls_back_to_the_ticketing_url(self):
        session = {"ticketingUrl": "https://apis.cineplex.com/x?VistaSessionId=99&LocationId=7420"}
        self.assertEqual(w.session_showtime_id(session), "99")

    def test_no_showtime_id_anywhere(self):
        self.assertEqual(w.session_showtime_id({"auditorium": "1"}), "")

    def test_taken_statuses(self):
        for status in ("Sold", "SOLD_OUT", "occupied", "Unavailable", "broken", True):
            self.assertTrue(w.seat_is_taken(status), status)

    def test_free_statuses(self):
        for status in ("Available", "empty", "0", None, False, "OK"):
            self.assertFalse(w.seat_is_taken(status), status)

    def test_availability_map_from_a_dict(self):
        self.assertEqual(w.availability_map({"seatAvailabilities": {"A1": "Sold"}}), {"A1": "Sold"})

    def test_availability_map_from_a_list(self):
        got = w.availability_map({"seats": [{"id": "A1", "status": "Sold"}]})
        self.assertEqual(got, {"A1": "Sold"})

    def test_availability_map_from_a_bare_mapping(self):
        self.assertEqual(w.availability_map({"A1": "Sold"}), {"A1": "Sold"})

    def test_availability_map_on_junk(self):
        self.assertEqual(w.availability_map(None), {})
        self.assertEqual(w.availability_map([]), {})

    def test_describe_seats_reads_like_an_instruction(self):
        text = w.describe_seats({"row": "J", "seats": ["8", "9", "10", "11", "12"], "rowFraction": 0.633})
        self.assertIn("row J", text)
        self.assertIn("8-12", text)
        self.assertIn("63% back", text)


class TestSeatLookupIsFailSoft(unittest.TestCase):
    """A seat lookup that fails must never cost the alert itself."""

    def setUp(self):
        self._real = w.http_get_json

    def tearDown(self):
        w.http_get_json = self._real

    def test_a_failing_seat_api_reports_unreadable_and_does_not_raise(self):
        def boom(*a, **k):
            raise OSError("ticketing API down")

        w.http_get_json = boom
        report = w.best_seats_for("7420", {"vistaSessionId": "1"}, SEAT_CFG)
        self.assertFalse(report["readable"])
        self.assertEqual(report["blocks"], [])

    def test_a_session_with_no_showtime_id_is_skipped_quietly(self):
        self.assertFalse(w.best_seats_for("7420", {}, SEAT_CFG)["readable"])


class TestRealSessionShape(unittest.TestCase):
    """Field names and types confirmed from a live run on 2026-08-26.

    Cineplex labels the format experienceTypes: ["IMAX", "70mm"], and the
    ordinary showings of the same film ["Regular"].
    """

    LIVE = {
        "seatMapUrl": "https://www.cineplex.com/en-Mobile/ticketing/preview?theatreId=7408&showtimeId=539537&dbox=False",
        "ticketingUrl": "https://apis.cineplex.com/prod/ticketing/api/v1/routing/redirect-to-ticketing?VistaSessionId=539537&LocationId=7408",
        "deeplinkUrl": "https://apis.cineplex.com/prod/cpx/theatrical/deeplink?s=539537&a=0000000001&l=7408&m=the-odyssey&ss=False",
        "vistaSessionId": 539537,
        "showStartDateTime": "2026-08-26T11:00:00",
        "showStartDateTimeUtc": "2026-08-26T15:00:00Z",
        "isInThePast": False,
        "isReservedSeating": True,
        "isShowtimeEnabledOnline": True,
        "seatsRemaining": 31,
        "isSoldOut": False,
        "auditorium": "IMAX",
    }

    def live_payload(self, experience_types, date="2026-09-17"):
        return {
            "theatreId": "7408",
            "dates": [{
                "startDate": f"{date}T00:00:00",
                "movies": [{
                    "id": "the-odyssey",
                    "name": "The Odyssey",
                    "experiences": [{
                        "experienceTypes": experience_types,
                        "sessions": [dict(self.LIVE, showStartDateTime=f"{date}T11:00:00")],
                    }],
                }],
            }],
        }

    def test_the_real_imax_70mm_label_matches(self):
        hits = w.find_matches(self.live_payload(["IMAX", "70mm"]), "7408", "Vaughan", CONFIG)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["seatsRemaining"], 31)
        self.assertEqual(hits[0]["auditorium"], "IMAX")

    def test_the_real_regular_label_is_rejected(self):
        # Same film, ordinary screen. This is the false alarm that matters.
        self.assertEqual(w.find_matches(self.live_payload(["Regular"]), "7408", "V", CONFIG), [])

    def test_other_premium_formats_are_rejected(self):
        for types in (["UltraAVX", "3D", "D-BOX", "Dolby Atmos"], ["IMAX"], ["70mm"]):
            self.assertEqual(w.find_matches(self.live_payload(types), "7408", "V", CONFIG), [], types)

    def test_the_integer_session_id_becomes_the_dedup_key(self):
        hits = w.find_matches(self.live_payload(["IMAX", "70mm"]), "7408", "V", CONFIG)
        self.assertEqual(hits[0]["key"], "7408:539537")

    def test_the_booking_link_is_the_deeplink(self):
        hits = w.find_matches(self.live_payload(["IMAX", "70mm"]), "7408", "V", CONFIG)
        self.assertTrue(hits[0]["url"].startswith("https://apis.cineplex.com/prod/cpx/theatrical/deeplink"))

    def test_the_seat_lookup_finds_the_integer_showtime_id(self):
        self.assertEqual(w.session_showtime_id(self.LIVE), "539537")

    def test_the_key_is_stable_across_runs(self):
        first = w.find_matches(self.live_payload(["IMAX", "70mm"]), "7408", "V", CONFIG)
        second = w.find_matches(self.live_payload(["IMAX", "70mm"]), "7408", "V", CONFIG)
        self.assertEqual(first[0]["key"], second[0]["key"])

    def test_a_session_with_no_id_still_gets_a_usable_key(self):
        bare = {k: v for k, v in self.LIVE.items()
                if k not in ("vistaSessionId", "seatMapUrl", "ticketingUrl", "deeplinkUrl")}
        key = w.session_key("7408", "2026-09-17", bare)
        self.assertEqual(key, "7408:2026-09-17:2026-08-26T11:00:00")


class TestRehearsalIsSafe(unittest.TestCase):
    """A drill must never eat the real alert."""

    def test_it_writes_to_a_throwaway_ledger_not_the_real_one(self):
        _cfg, state = w.rehearsal_setup(CONFIG, None)
        self.assertNotEqual(state.resolve(), w.DEFAULT_STATE.resolve())
        self.assertIn("rehearsal-", str(state))

    def test_two_drills_do_not_share_a_ledger(self):
        _c1, first = w.rehearsal_setup(CONFIG, None)
        _c2, second = w.rehearsal_setup(CONFIG, None)
        self.assertNotEqual(first, second)

    def test_the_title_says_it_is_a_drill(self):
        cfg, _state = w.rehearsal_setup(CONFIG, None)
        self.assertTrue(cfg["label"].startswith("DRILL"))
        title, _b, _l = w.build_message(run(cfg), cfg)
        self.assertIn("DRILL", title)

    def test_it_targets_today_by_default(self):
        cfg, _state = w.rehearsal_setup(CONFIG, None)
        today = w.datetime.now(w.timezone.utc).date().isoformat()
        self.assertEqual(cfg["targetDates"], [today])

    def test_an_explicit_date_wins(self):
        cfg, _state = w.rehearsal_setup(CONFIG, ["2026-09-17"])
        self.assertEqual(cfg["targetDates"], ["2026-09-17"])

    def test_the_real_config_is_not_mutated(self):
        before = json.dumps(CONFIG, sort_keys=True)
        w.rehearsal_setup(CONFIG, ["2026-01-01"])
        self.assertEqual(json.dumps(CONFIG, sort_keys=True), before)

    def test_the_format_filter_is_unchanged_by_a_drill(self):
        # A drill that quietly widened the filter would prove nothing.
        cfg, _state = w.rehearsal_setup(CONFIG, None)
        self.assertEqual(cfg["formatMatchAll"], CONFIG["formatMatchAll"])
        self.assertEqual(cfg["formatMatchAny"], CONFIG["formatMatchAny"])
        self.assertEqual(cfg["seats"], CONFIG["seats"])


class TestSeatQuality(unittest.TestCase):
    """The ranker returns the best block that exists, which on a picked-over
    showing is the front row. Calling that 'best' with no caveat is how you
    end up sitting in row A for a three-hour 1.43:1 film."""

    def q(self, back, offset=0.0):
        return w.seat_quality({"rowFraction": back, "centreOffset": offset})

    def test_the_front_of_the_house_is_called_out(self):
        self.assertEqual(self.q(0.04), "front row")
        self.assertEqual(self.q(0.20), "front row")

    def test_slightly_forward_is_flagged_but_not_condemned(self):
        self.assertEqual(self.q(0.30), "a bit close")

    def test_the_sweet_spot_is_ideal(self):
        self.assertEqual(self.q(0.65), "ideal")
        self.assertEqual(self.q(0.50), "ideal")

    def test_the_back_wall_is_called_out(self):
        self.assertEqual(self.q(0.95), "very back")

    def test_a_good_row_off_to_one_side_is_not_ideal(self):
        self.assertEqual(self.q(0.65, offset=0.6), "good row, off-centre")


class TestAlertFormatting(unittest.TestCase):
    def test_seat_labels_do_not_repeat_the_row(self):
        block = {"row": "H", "seats": ["H8", "H9", "H10"], "rowFraction": 0.6, "centreOffset": 0}
        self.assertEqual(w.format_block(block), "H8-H10")

    def test_bare_numeric_labels_get_the_row_prefixed(self):
        block = {"row": "H", "seats": ["8", "9", "10"], "rowFraction": 0.6, "centreOffset": 0}
        self.assertEqual(w.format_block(block), "H8-H10")

    def test_the_chain_boilerplate_is_dropped(self):
        self.assertEqual(w.short_theatre("Cineplex Cinemas Vaughan"), "Vaughan")
        self.assertEqual(w.short_theatre("Scotiabank Theatre Bayers Lake"), "Bayers Lake")
        self.assertEqual(w.short_theatre("The Kramer IMAX"), "The Kramer IMAX")

    def _hit(self, **kw):
        base = {"theatre": "Cineplex Cinemas Vaughan", "theatreId": "7408",
                "date": "2026-09-17", "start": "2026-09-17T19:00:00",
                "seatsRemaining": 300, "isSoldOut": False, "url": "https://x",
                "movie": "The Odyssey", "format": "IMAX 70mm", "key": "k"}
        base.update(kw)
        return base

    def test_a_good_block_reads_as_a_seat_instruction(self):
        report = {"readable": True, "largest": 20, "blocks": [
            {"row": "H", "seats": ["H10", "H11", "H12", "H13", "H14", "H15"],
             "rowFraction": 0.62, "centreOffset": 0.05}]}
        line = w.showtime_line(self._hit(seatReport=report), 6)
        self.assertIn("H10-H15", line)
        self.assertIn("ideal", line)
        self.assertIn("(300 left)", line)

    def test_no_block_for_the_party_says_so_and_names_the_longest_run(self):
        report = {"readable": True, "largest": 4, "blocks": []}
        line = w.showtime_line(self._hit(seatReport=report), 6)
        self.assertIn("no 6 together", line)
        self.assertIn("longest run 4", line)

    def test_an_unreadable_seat_map_is_distinct_from_no_seats(self):
        line = w.showtime_line(self._hit(seatReport={"readable": False, "blocks": [], "largest": 0}), 6)
        self.assertIn("unreadable", line)
        self.assertNotIn("no 6 together", line)

    def test_sold_out_short_circuits(self):
        line = w.showtime_line(self._hit(isSoldOut=True, seatReport={}), 6)
        self.assertIn("SOLD OUT", line)

    def test_showtimes_are_grouped_under_each_theatre(self):
        hits = [
            self._hit(theatre="Cineplex Cinemas Vaughan", start="2026-09-17T19:00:00", seatReport={}),
            self._hit(theatre="Cineplex Cinemas Mississauga Square One",
                      start="2026-09-17T13:30:00", seatReport={}),
            self._hit(theatre="Cineplex Cinemas Vaughan", start="2026-09-17T13:00:00", seatReport={}),
        ]
        _title, body, _link = w.build_message(hits, CONFIG)
        self.assertEqual(body.count("VAUGHAN"), 1)
        self.assertEqual(body.count("MISSISSAUGA SQUARE ONE"), 1)
        # Chronological within a theatre.
        vaughan = body.split("VAUGHAN")[1]
        self.assertLess(vaughan.index("1:00 PM"), vaughan.index("7:00 PM"))

    def test_the_header_states_the_party_size_and_the_date(self):
        _t, body, _l = w.build_message([self._hit(seatReport={})], CONFIG)
        self.assertIn("6 together", body)
        self.assertIn("Thu 17 Sep", body)

    def test_the_title_counts_the_showtimes(self):
        title, _b, _l = w.build_message([self._hit(seatReport={})], CONFIG)
        self.assertIn("1 showtime", title)
        self.assertNotIn("1 showtimes", title)


class TestRealSeatMaps(unittest.TestCase):
    """The seat maps as Cineplex actually returns them.

    Everything above this point runs on a synthetic auditorium, which only ever
    proves the ranker is self-consistent. These two are verbatim captures of
    `seat-layout` and `seat-availability` for real showings at Mississauga
    Square One -- one IMAX 70mm house, one UltraAVX house with a D-BOX block --
    and they are what proves the ranker can read the real thing.
    """

    @staticmethod
    def house(name):
        layout = json.loads((ROOT / "tests" / "fixtures" / f"seat_layout_{name}.json").read_text())
        avail = json.loads((ROOT / "tests" / "fixtures" / f"seat_availability_{name}.json").read_text())
        return layout, avail

    def setUp(self):
        self.imax = self.house("imax70")
        self.dbox = self.house("dbox")

    # -- the shape of the real response ------------------------------------

    def test_availability_is_keyed_by_the_seat_ids_the_layout_uses(self):
        layout, avail = self.imax
        statuses = w.availability_map(avail)
        ids = {s["id"] for _n, _a, rows in w.seat_areas(layout) for r in rows for s in r["seats"]}
        self.assertEqual(len(ids), 263)
        self.assertEqual(ids - set(statuses), set(), "layout seats missing from availability")

    def test_the_real_statuses_are_read_correctly(self):
        seen = set()
        for layout, avail in (self.imax, self.dbox):
            del layout
            seen |= set(w.availability_map(avail).values())
        self.assertEqual(seen, {"Available", "Occupied", "Broken"})
        self.assertFalse(w.seat_is_taken("Available"))
        self.assertTrue(w.seat_is_taken("Occupied"))
        self.assertTrue(w.seat_is_taken("Broken"))

    def test_the_real_seat_types_cover_the_avoid_list(self):
        layout, _avail = self.dbox
        types = {s["type"] for _n, _a, rows in w.seat_areas(layout) for r in rows for s in r["seats"]}
        self.assertEqual(types, {"Standard", "Wheelchair", "Companion"})
        avoid = CONFIG["seats"]["avoidSeatTypes"]
        self.assertTrue(all(any(a in t.lower() for a in avoid) for t in types - {"Standard"}))

    def test_a_row_with_no_label_and_no_seats_does_not_break_the_ranker(self):
        layout, avail = self.imax
        rows = w.seat_areas(layout)[0][2]
        self.assertNotIn(None, [r["label"] for r in rows], "the spacer row should be dropped")
        self.assertTrue(w.rank_seat_blocks(layout, avail, CONFIG))

    # -- every seating area, not just the first ----------------------------

    def test_both_areas_of_the_ultraavx_house_are_read(self):
        layout, _avail = self.dbox
        areas = dict((n, rows) for n, _a, rows in w.seat_areas(layout))
        self.assertEqual(sorted(areas), ["dboxSeats", "standardSeats"])
        seats = sum(len(r["seats"]) for rows in areas.values() for r in rows)
        self.assertEqual(seats, 331, "the 28 D-BOX seats must not go missing")

    def test_the_dbox_block_is_the_centre_of_the_row_the_standard_area_only_stubs(self):
        layout, _avail = self.dbox
        areas = {n: (a, rows) for n, a, rows in w.seat_areas(layout)}
        std_area, std_rows = areas["standardSeats"]
        dbx_area, dbx_rows = areas["dboxSeats"]
        std_h = next(r for r in std_rows if r["label"] == "H")
        dbx_h = next(r for r in dbx_rows if r["label"] == "H")

        # Same physical row: four seats by the walls, ten in the middle.
        self.assertEqual(w.row_depth(std_area, std_h, 14), w.row_depth(dbx_area, dbx_h, 0))
        stubs = sorted(w.seat_x(std_area, s) for s in std_h["seats"])
        centre = sorted(w.seat_x(dbx_area, s) for s in dbx_h["seats"])
        self.assertLess(stubs[1], centre[0])
        self.assertGreater(stubs[2], centre[-1])

    def test_a_dbox_block_is_labelled_as_dbox_in_the_alert(self):
        layout, avail = self.dbox
        block = next(b for b in w.rank_seat_blocks(layout, avail, CONFIG) if b["area"] == "dboxSeats")
        self.assertIn("D-BOX", w.seat_quality(block))

    def test_an_ordinary_block_carries_no_premium_tag(self):
        layout, avail = self.imax
        self.assertNotIn("D-BOX", w.seat_quality(w.rank_seat_blocks(layout, avail, CONFIG)[0]))

    # -- the geometry the ranking depends on -------------------------------

    def test_the_house_frame_comes_from_the_house_not_the_widest_row(self):
        layout, _avail = self.imax
        rows, centre, half = w.house_frame(layout, w.seat_areas(layout))
        self.assertEqual((rows, centre, half), (11.0, 14.5, 14.5))

    def test_centring_is_measured_from_the_screen_not_from_the_ragged_row(self):
        """Row A runs columns 3-22 in a house 0-28, so its own midpoint sits a
        seat and a half left of the screen. Rank against that and you recommend
        the wrong seats."""
        layout, avail = self.imax
        _rows, centre, _half = w.house_frame(layout, w.seat_areas(layout))
        area, rows = next((a, r) for _n, a, r in w.seat_areas(layout))
        row_a = next(r for r in rows if r["label"] == "A")
        xs = [w.seat_x(area, s) for s in row_a["seats"]]
        self.assertAlmostEqual((min(xs) + max(xs)) / 2, centre - 1.5)

        # Of the blocks row A can seat, the ranker must prefer the one nearest
        # the screen centre -- not the one nearest row A's own lopsided middle.
        free = w.bookable_seats(area, row_a, w.availability_map(avail),
                                CONFIG["seats"]["avoidSeatTypes"])
        candidates = w.contiguous_blocks(free, CONFIG["seats"]["partySize"])
        self.assertGreater(len(candidates), 1)
        mid = lambda b: (w.seat_x(area, b[0]) + w.seat_x(area, b[-1])) / 2
        nearest = min(candidates, key=lambda b: abs(mid(b) - centre))
        row_relative = min(candidates, key=lambda b: abs(mid(b) - (min(xs) + max(xs)) / 2))
        self.assertNotEqual([s["label"] for s in nearest], [s["label"] for s in row_relative],
                            "this row must actually distinguish the two rules")

        best_in_a = next(b for b in w.rank_seat_blocks(layout, avail, CONFIG) if b["row"] == "A")
        self.assertEqual(best_in_a["seats"], w.seat_labels(nearest))

    def test_row_depth_runs_front_to_back_from_the_screen(self):
        layout, _avail = self.imax
        area, rows = next((a, r) for _n, a, r in w.seat_areas(layout))
        depths = {r["label"]: w.row_depth(area, r, i) for i, r in enumerate(rows)}
        self.assertLess(depths["A"], depths["E"])
        self.assertLess(depths["E"], depths["J"])

    # -- the answer --------------------------------------------------------

    def test_the_party_of_six_gets_six_adjacent_real_seats(self):
        layout, avail = self.dbox
        statuses = w.availability_map(avail)
        labels = {s["label"]: s for _n, _a, rows in w.seat_areas(layout)
                  for r in rows for s in r["seats"]}
        for block in w.rank_seat_blocks(layout, avail, CONFIG)[:25]:
            self.assertEqual(len(block["seats"]), 6)
            for label in block["seats"]:
                self.assertIn(label, labels, "recommended a seat that does not exist")
                self.assertFalse(w.seat_is_taken(statuses.get(labels[label]["id"])))
                self.assertEqual(labels[label]["type"], "Standard")

    def test_the_top_block_of_the_ultraavx_house_is_where_you_would_choose_to_sit(self):
        layout, avail = self.dbox
        best = w.rank_seat_blocks(layout, avail, CONFIG)[0]
        self.assertEqual(w.seat_quality(best), "ideal")
        self.assertAlmostEqual(best["rowFraction"], 0.65, delta=0.08)
        self.assertLess(best["centreOffset"], 0.1)

    def test_a_picked_over_house_is_reported_honestly_rather_than_flattered(self):
        """39 of 263 seats left, all of them down the front. Six together only
        exists in rows A and B, and the alert has to say so."""
        layout, avail = self.imax
        best = w.rank_seat_blocks(layout, avail, CONFIG)[0]
        self.assertIn(best["row"], ("A", "B"))
        self.assertEqual(w.seat_quality(best), "front row")

    def test_the_longest_run_is_the_real_one(self):
        layout, avail = self.imax
        self.assertEqual(w.largest_run(layout, avail, CONFIG), 10)

    def test_seat_labels_are_printed_in_seating_order(self):
        for name in ("imax70", "dbox"):
            layout, avail = self.house(name)
            for block in w.rank_seat_blocks(layout, avail, CONFIG):
                numbers = [int(s.lstrip(block["row"])) for s in block["seats"]]
                self.assertEqual(numbers, sorted(numbers), f"{name}: {block['seats']}")


class TestTheatreCatalogue(unittest.TestCase):
    """`nearbyTheatres` is computed from the caller's IP, so reading only the
    first list in the response returns a handful of theatres that depend on
    where the run happens to execute -- and drops a configured one silently."""

    PAYLOAD = {
        "favouriteTheatres": [],
        "nearbyTheatres": [{"theatreId": 7408, "theatreName": "Cineplex Cinemas Vaughan"}],
        "otherTheatres": [
            {"theatreId": 7420, "theatreName": "Cineplex Cinemas Mississauga Square One"},
            {"theatreId": 7408, "theatreName": "Cineplex Cinemas Vaughan"},
        ],
    }

    class Stub:
        def __init__(self, payload):
            self.payload = payload

        def get(self, _path, _params):
            return self.payload

    def test_every_list_in_the_response_is_read(self):
        got = w.list_theatres(self.Stub(self.PAYLOAD))
        self.assertEqual([t["theatreId"] for t in got], [7408, 7420])

    def test_a_theatre_listed_twice_is_only_counted_once(self):
        self.assertEqual(len(w.list_theatres(self.Stub(self.PAYLOAD))), 2)

    def test_both_configured_theatres_resolve(self):
        resolved = w.resolve_theatres(self.Stub(self.PAYLOAD), CONFIG)
        self.assertEqual(sorted(tid for tid, _n in resolved), ["7408", "7420"])

    def test_an_unrecognisable_response_is_empty_rather_than_an_error(self):
        self.assertEqual(w.list_theatres(self.Stub({"error": "nope"})), [])


def block(row="G", first=12, size=6, score=0.01, area="standardSeats"):
    return {"row": row, "area": area, "score": score, "rowFraction": 0.65, "centreOffset": 0.03,
            "seats": [f"{row}{n}" for n in range(first, first + size)]}


class TestEscalationTarget(unittest.TestCase):
    """Which block is worth waking someone up for."""

    SPEC = CONFIG["escalate"]

    def test_the_seats_we_actually_want_qualify(self):
        for row in ("F", "G"):
            for first in (11, 12, 13, 14):
                self.assertTrue(w.block_is_target(block(row, first), self.SPEC), f"{row}{first}")

    def test_the_wrong_row_does_not_qualify(self):
        for row in ("A", "E", "H", "J"):
            self.assertFalse(w.block_is_target(block(row, 12), self.SPEC), row)

    def test_a_block_running_past_the_span_does_not_qualify(self):
        self.assertFalse(w.block_is_target(block("G", 15), self.SPEC))  # G15-G20
        self.assertFalse(w.block_is_target(block("G", 10), self.SPEC))  # G10-G15

    def test_the_span_is_wider_than_the_party_on_purpose(self):
        """A rule narrow enough to name one exact block goes silent the moment a
        single seat at its edge is taken."""
        self.assertTrue(w.block_is_target(block("G", 12), self.SPEC))
        self.assertTrue(w.block_is_target(block("G", 13), self.SPEC))
        self.assertTrue(w.block_is_target(block("G", 11), self.SPEC))

    def test_no_spec_means_nothing_is_ever_a_target(self):
        self.assertFalse(w.block_is_target(block(), {}))
        self.assertFalse(w.block_is_target(block(), None))

    def test_labels_with_no_number_cannot_be_judged_against_a_span(self):
        odd = {"row": "G", "seats": ["GW", "GW", "GW", "GW", "GW", "GW"]}
        self.assertFalse(w.block_is_target(odd, self.SPEC))

    def test_the_row_is_matched_case_insensitively(self):
        self.assertTrue(w.block_is_target(block("g", 12), self.SPEC))

    def test_the_real_imax_house_offers_a_target_when_it_opens(self):
        """The whole point: on an empty Sept 17 house, F/G 11-19 is there."""
        layout = json.loads((ROOT / "tests" / "fixtures" / "seat_layout_imax70.json").read_text())
        ranked = w.rank_seat_blocks(layout, {}, CONFIG)
        target = next((b for b in ranked if w.block_is_target(b, self.SPEC)), None)
        self.assertIsNotNone(target)
        self.assertIn(target["row"], ("F", "G"))

    def test_the_picked_over_house_offers_no_target(self):
        """Same house as it is today -- row A only. Nothing to shout about."""
        layout = json.loads((ROOT / "tests" / "fixtures" / "seat_layout_imax70.json").read_text())
        avail = json.loads((ROOT / "tests" / "fixtures" / "seat_availability_imax70.json").read_text())
        ranked = w.rank_seat_blocks(layout, avail, CONFIG)
        self.assertFalse([b for b in ranked if w.block_is_target(b, self.SPEC)])


class TestSubscriptionKey(unittest.TestCase):
    """A pinned key is a floor under the scraper, not a substitute for it.

    Getting this order wrong is subtle: overriding with a pinned key looks like
    the fast, safe choice, and is in fact the only version that can go stale."""

    def setUp(self):
        self._env, self._discover = dict(os.environ), w.discover_subscription_key
        os.environ.pop("CINEPLEX_API_KEY", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        w.discover_subscription_key = self._discover

    def test_the_scraped_key_wins_because_it_is_always_current(self):
        os.environ["CINEPLEX_API_KEY"] = "stale" * 6
        w.discover_subscription_key = lambda *a, **k: "fresh"
        self.assertEqual(w.get_subscription_key(), "fresh")

    def test_a_broken_scraper_falls_back_instead_of_going_dark(self):
        os.environ["CINEPLEX_API_KEY"] = "pinned"
        def boom(*a, **k):
            raise RuntimeError("no JS chunk referenced the theatrical API")
        w.discover_subscription_key = boom
        self.assertEqual(w.get_subscription_key(), "pinned")

    def test_with_no_fallback_a_broken_scraper_is_loud(self):
        def boom(*a, **k):
            raise RuntimeError("bundles moved")
        w.discover_subscription_key = boom
        with self.assertRaises(RuntimeError):
            w.get_subscription_key()

    def test_an_empty_fallback_is_not_a_fallback(self):
        os.environ["CINEPLEX_API_KEY"] = "   "
        def boom(*a, **k):
            raise RuntimeError("bundles moved")
        w.discover_subscription_key = boom
        with self.assertRaises(RuntimeError):
            w.get_subscription_key()


class TestEscalationWindow(unittest.TestCase):
    """The shouting window is wall-clock time, not a count of polls -- the
    cadence lives at cron-job.org, where nothing here can see it change."""

    @staticmethod
    def ago(minutes):
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def test_a_fresh_escalation_is_inside_the_window(self):
        self.assertTrue(w.within_escalation_window(None, 120))
        self.assertTrue(w.within_escalation_window({"since": self.ago(0)}, 120))

    def test_an_escalation_still_inside_its_two_hours_keeps_going(self):
        self.assertTrue(w.within_escalation_window({"since": self.ago(119)}, 120))

    def test_an_escalation_past_its_window_stands_down(self):
        self.assertFalse(w.within_escalation_window({"since": self.ago(121)}, 120))

    def test_the_window_does_not_change_when_the_poll_rate_does(self):
        """The whole point: 24 repeats meant 2 hours at a 5-minute poll and 24
        minutes at a 1-minute poll. Minutes mean minutes at any cadence."""
        entry = {"since": self.ago(90)}
        self.assertTrue(w.within_escalation_window(entry, 120))
        self.assertFalse(w.within_escalation_window(entry, 60))

    def test_an_unreadable_timestamp_errs_towards_still_alerting(self):
        for entry in ({"since": "not a date"}, {"since": None}, {}):
            self.assertTrue(w.within_escalation_window(entry, 120), entry)


class TestGoMessage(unittest.TestCase):
    def hit(self, key="k1", start="2026-09-17T19:00:00", target=None, theatre="Cineplex Cinemas Vaughan"):
        return {"key": key, "theatreId": "7408", "theatre": theatre, "date": "2026-09-17",
                "start": start, "movie": "The Odyssey", "format": "IMAX 70mm",
                "seatsRemaining": 269, "isSoldOut": False,
                "url": f"https://apis.cineplex.com/deeplink?s={key}",
                "seatReport": {"readable": True, "blocks": [], "largest": 29,
                               "target": target or block()}}

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["NTFY_TOPIC"] = "test-topic"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_the_title_says_which_seats_and_is_unmistakable(self):
        title, _b, _l, _a = w.go_message([self.hit()], CONFIG)
        self.assertIn("BUY NOW", title)
        self.assertIn("G12-G17", title)

    def test_the_link_is_the_deeplink_of_the_best_showtime(self):
        worse = self.hit(key="k2", start="2026-09-17T13:00:00", target=block("F", 14, score=0.9))
        better = self.hit(key="k1", start="2026-09-17T19:00:00", target=block("G", 12, score=0.01))
        _t, _b, link, actions = w.go_message([worse, better], CONFIG)
        self.assertEqual(link, better["url"])
        self.assertEqual(actions[0], {"action": "view", "label": "Buy now",
                                      "url": better["url"], "clear": True})

    def test_the_body_marks_the_one_to_tap(self):
        worse = self.hit(key="k2", start="2026-09-17T13:00:00", target=block("F", 14, score=0.9))
        better = self.hit(key="k1", start="2026-09-17T19:00:00", target=block("G", 12, score=0.01))
        _t, body, _l, _a = w.go_message([worse, better], CONFIG)
        marked = [ln for ln in body.splitlines() if "tap Buy now" in ln]
        self.assertEqual(len(marked), 1)
        self.assertIn("G12-G17", marked[0])

    def test_it_does_not_pretend_the_seats_are_held(self):
        _t, body, _l, _a = w.go_message([self.hit()], CONFIG)
        self.assertIn("Nothing is held", body)

    def test_there_is_a_one_tap_way_to_stop_the_shouting(self):
        _t, _b, _l, actions = w.go_message([self.hit()], CONFIG)
        ack = [a for a in actions if a["label"] == "Got it"][0]
        self.assertEqual(ack["method"], "POST")
        self.assertTrue(ack["url"].endswith("/test-topic-ack"))

    def test_with_no_ntfy_topic_there_is_no_ack_button(self):
        del os.environ["NTFY_TOPIC"]
        _t, _b, _l, actions = w.go_message([self.hit()], CONFIG)
        self.assertEqual([a["label"] for a in actions], ["Buy now"])


class TestAcknowledgement(unittest.TestCase):
    def setUp(self):
        self._env, self._get = dict(os.environ), w.http_get
        os.environ["NTFY_TOPIC"] = "test-topic"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        w.http_get = self._get

    def test_a_message_on_the_ack_topic_counts_as_acknowledged(self):
        w.http_get = lambda url, headers=None, timeout=30: (
            b'{"id":"x","event":"message","message":"ack"}\n')
        self.assertTrue(w.acknowledged_since("2026-09-17T00:00:00+00:00"))

    def test_an_empty_ack_topic_is_not_an_acknowledgement(self):
        w.http_get = lambda url, headers=None, timeout=30: b""
        self.assertFalse(w.acknowledged_since("2026-09-17T00:00:00+00:00"))

    def test_keepalives_are_not_acknowledgements(self):
        w.http_get = lambda url, headers=None, timeout=30: (
            b'{"id":"x","event":"keepalive"}\n{"id":"y","event":"open"}\n')
        self.assertFalse(w.acknowledged_since("2026-09-17T00:00:00+00:00"))

    def test_an_unreachable_ntfy_fails_closed(self):
        """An ack that cannot be read must never silence an alert nobody saw."""
        def boom(*a, **k):
            raise OSError("ntfy down")
        w.http_get = boom
        self.assertFalse(w.acknowledged_since("2026-09-17T00:00:00+00:00"))

    def test_no_topic_configured_is_never_acknowledged(self):
        del os.environ["NTFY_TOPIC"]
        self.assertFalse(w.acknowledged_since("2026-09-17T00:00:00+00:00"))


class TestEscalationLoop(unittest.TestCase):
    """The state machine, as it runs: a fresh container every five minutes that
    remembers nothing except `state/seen.json`."""

    def setUp(self):
        self.sent = []
        self.acked = False
        self._notify, self._ack = w.notify, w.acknowledged_since
        w.notify = lambda t, b, l, actions=None: self.sent.append((t, b, l, actions)) or 1
        w.acknowledged_since = lambda since: self.acked
        self.dir = tempfile.mkdtemp(prefix="escal-")
        self.state_path = Path(self.dir) / "seen.json"

    def tearDown(self):
        w.notify, w.acknowledged_since = self._notify, self._ack
        shutil.rmtree(self.dir, ignore_errors=True)

    def hit(self, key="k1", target=True):
        report = {"readable": True, "blocks": [block()], "largest": 29,
                  "target": block() if target else None}
        return {"key": key, "theatreId": "7408", "theatre": "Cineplex Cinemas Vaughan",
                "date": "2026-09-17", "start": "2026-09-17T19:00:00", "movie": "The Odyssey",
                "format": "IMAX 70mm", "seatsRemaining": 269, "isSoldOut": False,
                "url": "https://apis.cineplex.com/deeplink?s=1", "seatReport": report}

    def poll(self, hits, fresh=None, state=None):
        state = state if state is not None else w.load_state(self.state_path)
        escalating = dict(state.get("escalating") or {})
        already = set(state.get("alerted") or [])
        fresh = [h for h in hits if h["key"] not in already] if fresh is None else fresh
        w.dispatch(hits, fresh, escalating, CONFIG, state, self.state_path, already, False)
        return w.load_state(self.state_path)

    def test_the_first_sighting_of_the_target_seats_escalates(self):
        state = self.poll([self.hit()])
        self.assertIn("BUY NOW", self.sent[0][0])
        self.assertEqual(state["escalating"]["k1"]["sent"], 1)
        self.assertEqual(state["escalating"]["k1"]["seats"], "G12-G17")

    def test_it_keeps_shouting_while_the_seats_are_there_and_nobody_answers(self):
        state = self.poll([self.hit()])
        for expected in (2, 3, 4):
            state = self.poll([self.hit()], state=state)
            self.assertEqual(state["escalating"]["k1"]["sent"], expected)
        self.assertEqual(len(self.sent), 4)

    def test_one_tap_stops_every_showtime(self):
        state = self.poll([self.hit("k1"), self.hit("k2")])
        self.assertEqual(len(state["escalating"]), 2)
        self.acked = True
        state = self.poll([self.hit("k1"), self.hit("k2")], state=state)
        self.assertEqual(state["escalating"], {})

    def test_after_acknowledgement_it_stays_quiet(self):
        state = self.poll([self.hit()])
        self.acked = True
        state = self.poll([self.hit()], state=state)
        before = len(self.sent)
        state = self.poll([self.hit()], state=state)
        self.assertEqual(len(self.sent), before, "an acknowledged alert must not restart")

    def test_seats_lost_mid_escalation_are_reported_not_swallowed(self):
        state = self.poll([self.hit()])
        state = self.poll([self.hit(target=False)], state=state)
        self.assertEqual(state["escalating"], {})
        self.assertIn("gone", self.sent[-1][0].lower())

    def test_escalation_gives_up_after_the_configured_limit(self):
        state = w.load_state(self.state_path)
        state["escalating"] = {"k1": {"since": "2020-01-01T00:00:00+00:00", "sent": 3}}
        state["alerted"] = ["k1"]
        before = len(self.sent)
        state = self.poll([self.hit()], state=state)
        self.assertEqual(state["escalating"], {})
        self.assertEqual(len(self.sent), before, "a spent escalation must go quiet, not loop")

    def test_a_later_showtime_can_still_escalate_after_an_earlier_one_was_acknowledged(self):
        """The ack settles what was shouting at the time, not the whole season.
        A showtime Cineplex adds afterwards is news again."""
        state = self.poll([self.hit("k1")])
        self.acked = True
        state = self.poll([self.hit("k1")], state=state)
        self.acked = False
        before = len(self.sent)
        state = self.poll([self.hit("k1"), self.hit("k2")], state=state)
        self.assertEqual(len(self.sent), before + 1)
        self.assertEqual(list(state["escalating"]), ["k2"])

    def test_a_spent_escalation_does_not_restart_on_the_next_poll(self):
        state = w.load_state(self.state_path)
        state["escalating"] = {"k1": {"since": "2020-01-01T00:00:00+00:00", "sent": 3}}
        state["alerted"] = ["k1"]
        state = self.poll([self.hit()], state=state)
        before = len(self.sent)
        state = self.poll([self.hit()], state=state)
        self.assertEqual(len(self.sent), before)
        self.assertEqual(state["escalating"], {})

    def test_a_showtime_with_no_target_seats_gets_the_ordinary_alert(self):
        state = self.poll([self.hit(target=False)])
        self.assertNotIn("BUY NOW", self.sent[0][0])
        self.assertEqual(state["escalating"], {})
        self.assertEqual(state["alerted"], ["k1"])

    def test_an_escalating_showtime_is_re_examined_even_though_it_is_not_new(self):
        """The seat map has to be re-read each poll; a showtime already
        announced is exactly the one under escalation."""
        state = self.poll([self.hit()])
        state = self.poll([self.hit()], fresh=[], state=state)
        self.assertEqual(state["escalating"]["k1"]["sent"], 2)

    def test_a_dry_run_shouts_at_nobody_and_records_nothing(self):
        state = w.load_state(self.state_path)
        w.dispatch([self.hit()], [self.hit()], {}, CONFIG, state, self.state_path, set(), True)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
