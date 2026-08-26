#!/usr/bin/env python3
"""
Cineplex showtime watcher.

Polls the Cineplex theatrical API for a given film, on given dates, in a given
format, at given theatres -- and pushes an alert the moment a matching showtime
becomes bookable.

Cineplex releases premium-format dates in waves rather than all at once, so the
useful signal is the transition from "no sessions" to "sessions exist". This
script tracks which sessions it has already reported and only alerts on new
ones, which also means later waves for the same date still get through.

Runs unattended from GitHub Actions. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

THEATRICAL_API = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"
HOMEPAGE = "https://www.cineplex.com/"
CHUNK_PATH_RE = re.compile(r"/next-static-files/_next/static/chunks/[A-Za-z0-9._/-]+\.js")
KEY_RE = re.compile(r"""Ocp-Apim-Subscription-Key["']?\s*[:=]\s*["']([0-9a-f]{32})["']""", re.I)
BARE_KEY_RE = re.compile(r"\b([0-9a-f]{32})\b")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "watch.config.json"
DEFAULT_STATE = ROOT / "state" / "seen.json"


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def decompress(raw: bytes, content_encoding: str | None) -> bytes:
    """Undo whatever transfer encoding the response arrived in.

    Cineplex gzips regardless of what Accept-Encoding asks for, so the magic
    bytes are trusted ahead of the header. A header claiming a compression the
    body does not actually use is ignored rather than fatal: the empty body
    Cineplex returns for a date with no showtimes is this watcher's normal
    state, and must never be the thing that takes a run down.
    """
    if not raw:
        return raw
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if "deflate" in (content_encoding or "").lower():
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except zlib.error:
                continue
    return raw


def fetch(url: str, headers: dict | None = None, timeout: int = 30):
    """GET a URL, returning (status, headers, decompressed body)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        headers_out = resp.headers
        status = getattr(resp, "status", 200)
    return status, headers_out, decompress(body, headers_out.get("Content-Encoding"))


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    return fetch(url, headers, timeout)[2]


def http_get_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    status, resp_headers, raw = fetch(
        url, {"Accept": "application/json", **(headers or {})}, timeout
    )
    ctype = resp_headers.get("Content-Type", "")

    if not raw.strip():
        # Cineplex answers a date with nothing on it with an empty body rather
        # than an empty array. That is a normal "nothing yet", not a failure,
        # and it must not take the run down -- the watcher's whole job is to
        # sit through weeks of exactly this until the date opens.
        log(f"    empty body (HTTP {status}, {ctype or 'no content-type'})")
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        log(f"    non-JSON body (HTTP {status}, {ctype}): {raw[:300]!r}")
        raise


def http_post(url: str, data: bytes, headers: dict | None = None, timeout: int = 20) -> int:
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


# --------------------------------------------------------------------------
# Subscription key
# --------------------------------------------------------------------------

def discover_subscription_key(max_chunks: int = 60) -> str:
    """Scrape the API key out of Cineplex's own Next.js bundles.

    The key is a static value the site ships to every browser. Re-deriving it
    each run means a key rotation on their side fixes itself instead of
    silently breaking the watcher.
    """
    home = http_get(HOMEPAGE).decode("utf-8", "replace")
    paths = list(dict.fromkeys(CHUNK_PATH_RE.findall(home)))
    if not paths:
        raise RuntimeError("no Next.js chunk URLs found on the Cineplex homepage")

    log(f"  scanning {min(len(paths), max_chunks)} of {len(paths)} JS chunks for the API key")
    relevant: list[str] = []
    for path in paths[:max_chunks]:
        url = urllib.parse.urljoin(HOMEPAGE, path)
        try:
            body = http_get(url, timeout=20).decode("utf-8", "replace")
        except Exception:
            continue
        if "theatrical/api" in body:
            relevant.append(body)
    if not relevant:
        raise RuntimeError("no JS chunk referenced the theatrical API")

    # Two passes, in order of confidence. A bare 32-hex string near the API URL
    # is a decent guess but could just as easily be a build hash, so only reach
    # for it once every properly labelled key has been ruled out. The bundle
    # carries several subscription keys for different Cineplex services, hence
    # the windowing around the theatrical URL rather than a whole-file search.
    for pattern in (KEY_RE, BARE_KEY_RE):
        for body in relevant:
            for anchor in (m.end() for m in re.finditer(r"theatrical/api", body)):
                found = pattern.search(body[max(0, anchor - 2000) : anchor + 2000])
                if found:
                    return found.group(1)
    raise RuntimeError("could not locate the theatrical subscription key in any chunk")


def get_subscription_key() -> str:
    """Scrape the key; fall back to a pinned one only if scraping fails.

    This order matters, and the obvious one is wrong. A pinned key is the part
    that goes stale -- Cineplex rotates it and a hardcoded copy is silently a
    run behind forever -- while scraping is self-correcting by construction and
    costs about a second against a sixty-second polling budget.

    What scraping cannot survive is Cineplex restructuring their bundles, which
    would take the watcher dark with no warning. That is the failure
    CINEPLEX_API_KEY exists for: a floor under the scraper, not a substitute
    for it. A stale fallback still costs nothing, because a rejected key is
    re-derived mid-run anyway.
    """
    try:
        return discover_subscription_key()
    except Exception as exc:
        fallback = (os.environ.get("CINEPLEX_API_KEY") or "").strip()
        if not fallback:
            raise
        log(f"  !! key discovery failed ({exc})")
        log("  !! falling back to CINEPLEX_API_KEY -- check whether the scraper needs fixing")
        return fallback


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class Api:
    """Thin client that re-derives its key if Cineplex rotates it.

    The key is a static value lifted from the site's own bundle, so it does go
    stale. Recovering in place beats a run that fails on a 401 and leaves the
    watch dark until someone notices.
    """

    def __init__(self, key: str, may_rediscover: bool = True):
        self.key = key
        self._may_rediscover = may_rediscover

    def get(self, path: str, params: dict) -> dict:
        url = f"{THEATRICAL_API}/{path}?{urllib.parse.urlencode(params)}"
        try:
            return http_get_json(url, {"Ocp-Apim-Subscription-Key": self.key})
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403) or not self._may_rediscover:
                raise
            log(f"  key rejected ({exc.code}) -- re-deriving it from the site bundle")
            self._may_rediscover = False  # one recovery attempt per run
            self.key = discover_subscription_key()
            return http_get_json(url, {"Ocp-Apim-Subscription-Key": self.key})


NAME_FIELDS = ("name", "theatreName", "displayName", "title", "locationName")
ID_FIELDS = ("id", "theatreId", "locationId", "theatreID")


def describe_shape(node, depth: int = 0) -> str:
    """A one-line sketch of a JSON value, for diagnosing an unexpected response."""
    if isinstance(node, dict):
        if depth >= 2:
            return "{...}"
        inner = ", ".join(f"{k}: {describe_shape(v, depth + 1)}" for k, v in list(node.items())[:12])
        return "{" + inner + ("}" if len(node) <= 12 else ", ...}")
    if isinstance(node, list):
        return f"[{len(node)} x {describe_shape(node[0], depth + 1) if node else 'empty'}]"
    return type(node).__name__


def looks_like_theatre(node) -> bool:
    return (
        isinstance(node, dict)
        and any(node.get(f) for f in NAME_FIELDS)
        and any(node.get(f) is not None for f in ID_FIELDS)
    )


def find_record_list(node, predicate, depth: int = 0) -> list | None:
    """Depth-first search for the first list whose entries satisfy `predicate`.

    Cineplex has moved this payload between a bare array and various envelopes.
    Searching for the records by their shape rather than by a fixed key means a
    re-wrapped response does not silently read as "zero theatres".
    """
    if depth > 6:
        return None
    if isinstance(node, list):
        if node and all(predicate(item) for item in node[:3]):
            return node
        for item in node:
            found = find_record_list(item, predicate, depth + 1)
            if found:
                return found
    elif isinstance(node, dict):
        for value in node.values():
            found = find_record_list(value, predicate, depth + 1)
            if found:
                return found
    return None


def find_all_record_lists(node, predicate, depth: int = 0) -> list[list]:
    """Every list whose entries satisfy `predicate`, not merely the first."""
    if depth > 6:
        return []
    if isinstance(node, list):
        if node and all(predicate(item) for item in node[:3]):
            return [node]
        out = []
        for item in node:
            out.extend(find_all_record_lists(item, predicate, depth + 1))
        return out
    if isinstance(node, dict):
        out = []
        for value in node.values():
            out.extend(find_all_record_lists(value, predicate, depth + 1))
        return out
    return []


def list_theatres(api: Api, language: str = "en") -> list[dict]:
    """The whole catalogue, not the first slice of it the response happens to hold.

    The response splits into `favouriteTheatres`, `nearbyTheatres` and
    `otherTheatres`, and "nearby" is computed from the caller's IP -- so taking
    the first list found returns four theatres out of a hundred and fifty-odd,
    varies with where the run happens to execute, and drops a configured
    theatre without ever saying it did.
    """
    payload = api.get("theatres", {"language": language})
    seen: set[str] = set()
    theatres: list[dict] = []
    for records in find_all_record_lists(payload, looks_like_theatre):
        for record in records:
            key = next((str(record[f]) for f in ID_FIELDS if record.get(f) is not None), "")
            if key and key not in seen:
                seen.add(key)
                theatres.append(record)
    if not theatres:
        log(f"  !! no theatre records found in the response; shape was {describe_shape(payload)}")
        log("  !! raw response (truncated): " + json.dumps(payload)[:1500])
    return theatres


def get_showtimes(api: Api, location_id: str, date_iso: str, language: str = "en") -> dict:
    return api.get("showtimes", {"language": language, "locationId": location_id, "date": date_iso})


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def flatten_text(value) -> str:
    """Collapse an arbitrarily shaped field into one lowercase string.

    `experienceTypes` has been seen as a list of strings and as a list of
    objects; matching on a flattened blob survives either shape.
    """
    parts: list[str] = []

    def walk(node) -> None:
        if node is None:
            return
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, (int, float)):
            parts.append(str(node))
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return " ".join(parts).lower()


def normalise(text: str) -> str:
    """Fold whitespace and punctuation so '70 mm' and '70-mm' match '70mm'."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def matches_all(haystack: str, needles: list[str]) -> bool:
    flat = normalise(haystack)
    return all(normalise(n) in flat for n in needles)


def matches_any(haystack: str, needles: list[str]) -> bool:
    if not needles:
        return True
    flat = normalise(haystack)
    return any(normalise(n) in flat for n in needles)


def field(node: dict, *names: str) -> str:
    for name in names:
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def date_prefix(value) -> str:
    if not isinstance(value, str):
        return ""
    return value[:10]


def looks_like_day(node) -> bool:
    return isinstance(node, dict) and isinstance(node.get("movies"), list)


def extract_days(payload) -> list[dict]:
    """Pull the per-date blocks out of a showtimes response.

    Same reasoning as find_record_list for theatres: match on the shape of the
    records so a re-wrapped envelope does not read as "no showtimes".
    """
    days = payload.get("dates") if isinstance(payload, dict) else None
    if isinstance(days, list) and days:
        return days
    return find_record_list(payload, looks_like_day) or []


def iter_sessions(payload: dict, target_dates: list[str]):
    """Yield (date, movie, experience, session) for the dates we care about."""
    for day in extract_days(payload):
        day_iso = date_prefix(day.get("startDate") or day.get("date"))
        if target_dates and day_iso not in target_dates:
            continue
        for movie in day.get("movies") or []:
            for experience in movie.get("experiences") or []:
                for session in experience.get("sessions") or []:
                    yield day_iso, movie, experience, session


def session_key(theatre_id: str, date_iso: str, session: dict) -> str:
    """Stable identity for one showing, used as the dedup ledger key.

    Live data returns vistaSessionId as an integer (539537), which the
    string-only `field` helper dropped -- silently falling back to the
    date-and-time composite. That still deduped correctly, but the id is the
    better key, so go through the same resolver the seat lookup uses.
    """
    showtime_id = session_showtime_id(session)
    if showtime_id:
        return f"{theatre_id}:{showtime_id}"
    return f"{theatre_id}:{date_iso}:{field(session, 'showStartDateTime')}"


def find_matches(payload: dict, theatre_id: str, theatre_name: str, cfg: dict) -> list[dict]:
    movie_match = cfg.get("movieMatch") or []
    fmt_all = cfg.get("formatMatchAll") or []
    fmt_any = cfg.get("formatMatchAny") or []
    target_dates = cfg.get("targetDates") or []
    require_bookable = cfg.get("requireBookable", True)

    hits: list[dict] = []
    for date_iso, movie, experience, session in iter_sessions(payload, target_dates):
        name = field(movie, "name", "title", "filmName", "movieName")
        if movie_match and not matches_any(name, movie_match):
            continue

        exp_blob = " ".join(
            [
                flatten_text(experience.get("experienceTypes")),
                flatten_text(experience.get("name")),
                flatten_text(experience.get("experienceName")),
            ]
        ).strip()
        # Cineplex sells the premium run under its own title -- "The Odyssey:
        # The IMAX Experience in 70MM Film" -- so the title alone cannot decide
        # format: a plain digital session can sit under that same title and
        # would sail through a title-based check. Judge on the experience
        # whenever it says anything at all, and fall back to the title only
        # when the experience is silent.
        fmt_blob = exp_blob or name
        if fmt_all and not matches_all(fmt_blob, fmt_all):
            continue
        if fmt_any and not matches_any(fmt_blob, fmt_any):
            continue

        if require_bookable and session.get("isShowtimeEnabledOnline") is False:
            continue

        hits.append(
            {
                "key": session_key(theatre_id, date_iso, session),
                "theatreId": theatre_id,
                "theatre": theatre_name,
                "date": date_iso,
                "movie": name,
                "format": field(experience, "name", "experienceName")
                or flatten_text(experience.get("experienceTypes")).upper(),
                "start": field(session, "showStartDateTime"),
                "auditorium": field(session, "auditorium"),
                "seatsRemaining": session.get("seatsRemaining"),
                "isSoldOut": bool(session.get("isSoldOut")),
                "session": session,
                "url": field(
                    session,
                    "deeplinkUrl",
                    "ticketingRedesignUrl",
                    "ticketingUrl",
                    "seatMapUrl",
                ),
            }
        )
    return hits


# --------------------------------------------------------------------------
# Seats
# --------------------------------------------------------------------------

TICKETING_API = "https://apis.cineplex.com/prod/ticketing/api/v1"
SHOWTIME_ID_RE = re.compile(r"(?:showtimeId|VistaSessionId)=(\d+)", re.I)
TAKEN_RE = re.compile(r"sold|taken|occupied|unavailable|reserved|broken|blocked|house", re.I)


def session_showtime_id(session: dict) -> str:
    """The Vista showtime id, which the ticketing API is keyed on.

    Falls back to the id embedded in the seat-map and ticketing URLs, because
    the field name carrying it is not guaranteed across responses.
    """
    for name in ("vistaSessionId", "showtimeId", "sessionId", "id"):
        value = session.get(name)
        if value not in (None, ""):
            return str(value)
    for name in ("seatMapUrl", "ticketingUrl", "ticketingRedesignUrl", "getTicketingUrlApi"):
        url = session.get(name)
        if isinstance(url, str):
            found = SHOWTIME_ID_RE.search(url)
            if found:
                return found.group(1)
    return ""


def looks_like_row(node) -> bool:
    return isinstance(node, dict) and isinstance(node.get("seats"), list) and bool(node.get("seats"))


def seat_is_taken(status) -> bool:
    """Treat anything that reads as occupied as taken; unknown means free.

    At the moment a date opens every seat is free, so the cost of the two
    mistakes is lopsided: hiding a good seat is worse than briefly offering
    one that has just gone.
    """
    if status is None:
        return False
    if isinstance(status, bool):
        return status
    return bool(TAKEN_RE.search(str(status)))


def availability_map(avail) -> dict:
    """seat id -> raw status, wherever the response happens to keep it."""
    if isinstance(avail, dict):
        for key in ("seatAvailabilities", "seatAvailability", "seats", "availability"):
            value = avail.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                out = {}
                for entry in value:
                    if isinstance(entry, dict):
                        sid = entry.get("id") or entry.get("seatId")
                        if sid is not None:
                            out[str(sid)] = entry.get("status", entry.get("state"))
                return out
        if avail and all(not isinstance(v, (dict, list)) for v in avail.values()):
            return avail
    return {}


def seat_column(seat) -> int | None:
    for name in ("column", "columnIndex", "number", "seatNumber", "x"):
        value = seat.get(name)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def seat_areas(layout) -> list[tuple[str, dict, list[dict]]]:
    """Every seating area in the layout, as (name, area, rows).

    A Cineplex house is not one grid. The response splits it into areas --
    `standardSeats`, `dboxSeats`, `balconySeats` -- each with its own rows and
    its own origin. Reading only the first one found is how the ten D-BOX seats
    in the dead centre of row H become invisible while the four stub seats out
    by the wall, which share that row label, are all the ranker can see.
    """
    found: list[tuple[str, dict, list[dict]]] = []

    def walk(name: str, node, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            rows = node.get("rows")
            if isinstance(rows, list) and any(looks_like_row(r) for r in rows):
                found.append((name, node, [r for r in rows if looks_like_row(r)]))
                return
            for key, value in node.items():
                walk(key, value, depth + 1)
        elif isinstance(node, list):
            if node and any(looks_like_row(r) for r in node):
                found.append((name, {}, [r for r in node if looks_like_row(r)]))
                return
            for item in node:
                walk(name, item, depth + 1)

    walk("seats", layout)
    return found


def number_of(node, name: str, fallback: float) -> float:
    value = node.get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback


def row_depth(area: dict, row: dict, index: int) -> float:
    """How far back a row sits, in rows, on the house's own scale.

    `number` is the row's index within its area and `top` is where that area
    starts, so the two together survive the areas being merged -- which plain
    list position does not, D-BOX row H being number 0 of its own area while
    sitting fourteen rows back in the house.
    """
    return number_of(area, "top", 0.0) + number_of(row, "number", float(index))


def seat_x(area: dict, seat: dict) -> float | None:
    """Where a seat sits across the house, in standard-seat widths.

    Areas do not share an origin or a seat width -- the D-BOX area starts eight
    columns in and its seats are 1.4x as wide -- so a raw column index is only
    comparable to another column index from the same area.
    """
    column = seat_column(seat)
    if column is None:
        return None
    return number_of(area, "left", 0.0) + (column + 0.5) * number_of(area, "columnWidth", 1.0)


def house_frame(layout, areas: list[tuple[str, dict, list[dict]]]) -> tuple[float, float, float]:
    """(rows deep, centre line, half width) for the whole auditorium.

    The centre line has to come from the house, not from the row: rows are
    ragged -- row A of the Square One IMAX runs columns 3-22 against a house of
    0-28 -- so a block centred in its own row sits a seat and a half left of
    the screen.
    Normalising by the house also makes one row's centre penalty mean the same
    as another's, which is the whole basis for ranking them against each other.
    """
    depths = [row_depth(area, row, i) for _n, area, rows in areas for i, row in enumerate(rows)]
    total_rows = number_of(layout, "totalRows", 0.0) if isinstance(layout, dict) else 0.0
    if total_rows <= 0:
        total_rows = (max(depths) + 1) if depths else 1.0

    total_columns = number_of(layout, "totalColumns", 0.0) if isinstance(layout, dict) else 0.0
    if total_columns > 0:
        return total_rows, total_columns / 2, max(1.0, total_columns / 2)

    xs = [x for _n, area, rows in areas for row in rows for s in row["seats"]
          if (x := seat_x(area, s)) is not None]
    if not xs:
        return total_rows, 0.0, 1.0
    return total_rows, (min(xs) + max(xs)) / 2, max(1.0, (max(xs) - min(xs)) / 2)


def bookable_seats(area: dict, row: dict, statuses: dict, avoid: list[str]) -> list[dict]:
    free = []
    for seat in row.get("seats") or []:
        sid = str(seat.get("id", seat.get("seatId", "")))
        if seat_is_taken(statuses.get(sid)):
            continue
        if avoid and any(a in str(seat.get("type", "")).lower() for a in avoid):
            continue
        if seat_x(area, seat) is None:
            continue
        free.append(seat)
    return free


def contiguous_blocks(seats: list[dict], size: int) -> list[list[dict]]:
    """Every run of `size` seats sitting side by side with no gap.

    A gap in the column numbering is an aisle, which is exactly where a block
    must not straddle, so consecutive columns are the requirement rather than
    merely adjacent list positions.
    """
    ordered = sorted(
        (s for s in seats if seat_column(s) is not None), key=lambda s: seat_column(s)
    )
    blocks = []
    for start in range(len(ordered) - size + 1):
        window = ordered[start : start + size]
        columns = [seat_column(s) for s in window]
        if columns[-1] - columns[0] == size - 1:
            blocks.append(window)
    return blocks


def seat_labels(block: list[dict]) -> list[str]:
    """Seat labels in seating order.

    The block is ordered by column, which for most rows runs the same way as
    the numbering -- but not all of them, and a block printed as H12-H7 reads
    like a mistake.
    """
    labels = [field(s, "label", "seatLabel", "name") or str(seat_column(s)) for s in block]
    numbers = [int(m.group(1)) for s in labels if (m := re.search(r"(\d+)$", s))]
    if len(numbers) == len(labels) and numbers == sorted(numbers, reverse=True):
        labels.reverse()
    return labels


def rank_seat_blocks(layout, avail, cfg: dict) -> list[dict]:
    """Rank every bookable block of `partySize` adjacent seats, best first.

    Scores on two axes drawn from how IMAX 1.43 is meant to be watched: a row
    around two thirds back, so the very tall frame sits inside your field of
    view without neck strain, and dead centre horizontally.
    """
    spec = cfg.get("seats") or {}
    size = int(spec.get("partySize", 2))
    target_frac = float(spec.get("targetRowFraction", 0.65))
    row_weight = float(spec.get("rowWeight", 1.0))
    center_weight = float(spec.get("centerWeight", 0.8))
    avoid = [a.lower() for a in (spec.get("avoidSeatTypes") or [])]

    areas = seat_areas(layout)
    if not areas:
        return []
    statuses = availability_map(avail)
    total_rows, centre, half_width = house_frame(layout, areas)

    ranked = []
    for name, area, rows in areas:
        for index, row in enumerate(rows):
            free = bookable_seats(area, row, statuses, avoid)
            row_frac = (row_depth(area, row, index) + 0.5) / total_rows
            row_penalty = abs(row_frac - target_frac)
            for block in contiguous_blocks(free, size):
                xs = [seat_x(area, s) for s in block]
                centre_penalty = abs((xs[0] + xs[-1]) / 2 - centre) / half_width
                ranked.append(
                    {
                        "row": field(row, "label", "rowLabel", "name") or str(index + 1),
                        "area": name,
                        "rowFraction": round(row_frac, 3),
                        "centreOffset": round(centre_penalty, 3),
                        "seats": seat_labels(block),
                        "score": round(row_weight * row_penalty + center_weight * centre_penalty, 4),
                    }
                )
    ranked.sort(key=lambda b: b["score"])
    return ranked


def largest_run(layout, avail, cfg: dict) -> int:
    """Longest stretch of adjacent bookable seats anywhere in the house.

    When no block of the full party size exists, this is what makes the
    difference between "the seat map broke" and "this showing cannot seat you
    together" -- two situations that otherwise look identical in the alert.
    """
    spec = cfg.get("seats") or {}
    avoid = [a.lower() for a in (spec.get("avoidSeatTypes") or [])]
    statuses = availability_map(avail)
    best = 0
    for _name, area, rows in seat_areas(layout):
        for row in rows:
            free = bookable_seats(area, row, statuses, avoid)
            columns = sorted(c for c in (seat_column(s) for s in free) if c is not None)
            run = 0
            previous = None
            for column in columns:
                run = run + 1 if previous is not None and column == previous + 1 else 1
                previous = column
                best = max(best, run)
    return best


def seat_numbers(block: dict) -> list[int]:
    """The trailing number of each seat label: ["G12", "G13"] -> [12, 13]."""
    out = []
    for label in block.get("seats") or []:
        found = re.search(r"(\d+)$", str(label))
        if found:
            out.append(int(found.group(1)))
    return out


def block_is_target(block: dict, spec: dict) -> bool:
    """Is this the block worth waking someone up for?

    A block has to clear both bars to qualify: the right rows, and far enough
    from the walls. The seat span is deliberately wider than the party -- with
    a span of 11-19 a party of six still qualifies at 12-17, 13-18 or 11-16 --
    because a rule narrow enough to name one exact block goes silent the moment
    a single seat at its edge is taken, which is the opposite of the point.
    """
    if not spec:
        return False
    rows = [str(r).upper() for r in (spec.get("rows") or [])]
    if rows and str(block.get("row", "")).upper() not in rows:
        return False
    span = spec.get("seats") or []
    if len(span) == 2:
        numbers = seat_numbers(block)
        if len(numbers) != len(block.get("seats") or []):
            return False  # unnumbered labels cannot be judged against a span
        if min(numbers) < int(span[0]) or max(numbers) > int(span[1]):
            return False
    return True


def describe_seats(block: dict) -> str:
    labels = block["seats"]
    span = f"{labels[0]}-{labels[-1]}" if len(labels) > 1 else labels[0]
    back = int(round(block["rowFraction"] * 100))
    return f"row {block['row']} seats {span} ({back}% back)"


def best_seats_for(theatre_id: str, session: dict, cfg: dict) -> dict:
    """Seat report for one showtime.

    Returns {"readable": bool, "blocks": [...], "largest": int}, which lets the
    alert distinguish three cases that would otherwise read the same: the seat
    map could not be fetched, it was fetched but nothing seats the whole party
    together, or here are the best seats.

    Never raises: a seat lookup that fails must not cost the alert itself.
    """
    blank = {"readable": False, "blocks": [], "largest": 0}
    showtime_id = session_showtime_id(session)
    if not showtime_id:
        return blank
    base = f"{TICKETING_API}/theatre/{theatre_id}/showtime/{showtime_id}"
    try:
        layout = http_get_json(f"{base}/seat-layout")
        avail = http_get_json(f"{base}/seat-availability")
    except Exception as exc:
        log(f"    seat map unavailable for showtime {showtime_id}: {exc}")
        return blank
    top = int((cfg.get("seats") or {}).get("topN", 3))
    ranked = rank_seat_blocks(layout, avail, cfg)
    spec = cfg.get("escalate") or {}
    # Searched over the whole ranking, not the top few: a block can clear the
    # escalation bar while sitting fourth on score, and truncating first would
    # lose exactly the showing worth shouting about.
    target = next((b for b in ranked if block_is_target(b, spec)), None)
    return {
        "readable": True,
        "blocks": ranked[:top],
        "target": target,
        "largest": largest_run(layout, avail, cfg),
    }


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

def pretty_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        return value or "time TBA"


def pretty_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%a %-d %b")
    except Exception:
        return value


def describe(hit: dict) -> str:
    bits = [pretty_time(hit["start"]), hit["theatre"]]
    if hit.get("auditorium"):
        bits.append(f"aud {hit['auditorium']}")
    if hit.get("isSoldOut"):
        bits.append("SOLD OUT")
    elif isinstance(hit.get("seatsRemaining"), int):
        bits.append(f"{hit['seatsRemaining']} seats left")
    return " · ".join(bits)


THEATRE_PREFIXES = (
    "Cineplex Cinemas ",
    "Cineplex Odeon ",
    "Scotiabank Theatre ",
    "Cinéma Banque Scotia ",
    "SilverCity ",
    "Galaxy Cinemas ",
)


def short_theatre(name: str) -> str:
    """Drop the chain boilerplate; on a phone the branch is the useful part."""
    for prefix in THEATRE_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def pretty_clock(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%-I:%M %p")
    except Exception:
        return value or "?"


AREA_TAGS = {"dbox": "D-BOX", "balcony": "balcony", "vip": "VIP", "lounger": "lounger"}


def area_tag(block: dict) -> str:
    """The premium area a block sits in, if it is not the ordinary seats.

    A D-BOX seat carries an upcharge and moves during the film. Recommending
    six of them as simply the best seats in the house is the same failure as
    recommending the front row without saying it is the front row.
    """
    name = str(block.get("area", "")).lower()
    return next((tag for key, tag in AREA_TAGS.items() if key in name), "")


def seat_quality(block: dict) -> str:
    """A plain verdict on a block, so a bad seat is never sold as a good one.

    The ranker returns the best block that exists, which on a nearly-full
    showing can be the front row. Saying so is the difference between useful
    and misleading.
    """
    back = block.get("rowFraction", 0)
    off_centre = block.get("centreOffset", 0) > 0.2
    if back < 0.25:
        verdict = "front row"
    elif back < 0.40:
        verdict = "a bit close"
    elif back > 0.85:
        verdict = "very back"
    elif off_centre:
        verdict = "good row, off-centre"
    else:
        verdict = "ideal"
    tag = area_tag(block)
    return f"{tag} · {verdict}" if tag else verdict


def format_block(block: dict) -> str:
    """"H8-H13". Seat labels often already carry the row, so do not repeat it."""
    labels = block["seats"]
    first, last = labels[0], labels[-1]
    row = str(block.get("row") or "")
    if row and first.upper().startswith(row.upper()):
        return f"{first}-{last}"
    return f"{row}{first}-{row}{last}"


def showtime_line(hit: dict, size: int | None) -> str:
    when = pretty_clock(hit["start"])
    left = hit.get("seatsRemaining")
    tail = f"  ({left} left)" if isinstance(left, int) else ""
    if hit.get("isSoldOut"):
        return f"{when:>8}  SOLD OUT"

    report = hit.get("seatReport") or {}
    blocks = report.get("blocks") or []
    if blocks:
        best = blocks[0]
        return f"{when:>8}  {format_block(best):<11} {seat_quality(best)}{tail}"
    if report.get("readable"):
        largest = report.get("largest", 0)
        run = f"longest run {largest}" if largest else "nothing adjacent"
        return f"{when:>8}  no {size} together ({run}){tail}"
    if size:
        return f"{when:>8}  seat map unreadable{tail}"
    return f"{when:>8}{tail}"


def build_message(hits: list[dict], cfg: dict) -> tuple[str, str, str]:
    label = cfg.get("label") or "Showtimes"
    size = (cfg.get("seats") or {}).get("partySize")
    count = len(hits)
    title = f"{label} — {count} showtime{'s' if count != 1 else ''}"

    dates = sorted({h["date"] for h in hits})
    when = pretty_date(dates[0]) if len(dates) == 1 else f"{len(dates)} dates"
    header = f"{size} together · {when}" if size else when

    grouped: dict[str, list[dict]] = {}
    for hit in hits:
        grouped.setdefault(short_theatre(hit["theatre"]), []).append(hit)

    lines = [header]
    for theatre, group in grouped.items():
        lines.append("")
        lines.append(theatre.upper())
        for hit in sorted(group, key=lambda h: h["start"]):
            lines.append(showtime_line(hit, size))

    link = next((h["url"] for h in hits if h.get("url")), cfg.get("fallbackUrl", HOMEPAGE))
    return title, "\n".join(lines), link


def within_escalation_window(entry: dict | None, minutes: float) -> bool:
    """Is this escalation still inside its allotted shouting time?

    Measured in wall-clock minutes rather than in polls. A count of repeats
    means something different at every polling rate -- twenty-four of them is
    two hours at a five-minute poll and twenty-four minutes at a one-minute
    poll -- so the cadence could be changed at the cron service, with no sign
    here, and quietly cut the alert window by a factor of five.
    """
    if not entry:
        return True
    try:
        started = datetime.fromisoformat(entry["since"])
    except (KeyError, TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() < minutes * 60


def go_message(hits: list[dict], cfg: dict) -> tuple[str, str, str, list]:
    """The alert for "the seats you asked for are sitting there right now".

    Deliberately a different shape from the routine alert: a different title,
    the one showtime to act on first, and a tap that lands on that showtime's
    seat picker. The routine alert is a digest to read; this one is an
    instruction to follow while half awake.
    """
    size = (cfg.get("seats") or {}).get("partySize") or 0
    # Best seats first; equal seats settled by the earlier showtime, so the
    # same drop always names the same showtime rather than whichever theatre
    # the API happened to answer first.
    ranked = sorted(hits, key=lambda h: (h["seatReport"]["target"]["score"], h["start"]))
    first = ranked[0]
    block = first["seatReport"]["target"]

    title = f"BUY NOW — {format_block(block)} open for {pretty_date(first['date'])}"

    lines = [
        f"{size} together in {format_block(block)} at "
        f"{short_theatre(first['theatre'])}, {pretty_clock(first['start'])}.",
        "",
    ]
    grouped: dict[str, list[dict]] = {}
    for hit in ranked:
        grouped.setdefault(short_theatre(hit["theatre"]), []).append(hit)
    for theatre, group in grouped.items():
        lines.append(theatre.upper())
        for hit in sorted(group, key=lambda h: h["start"]):
            mark = " <-- tap Buy now" if hit is first else ""
            lines.append(
                f"{pretty_clock(hit['start']):>8}  "
                f"{format_block(hit['seatReport']['target']):<11}"
                f"{seat_quality(hit['seatReport']['target'])}{mark}"
            )
        lines.append("")
    lines.append("Nothing is held for you -- you still pick the seats and pay.")
    lines.append('Tap "Got it" to stop these alerts.')

    actions = [{"action": "view", "label": "Buy now", "url": first["url"], "clear": True}]
    if ack_topic():
        actions.append(
            {
                "action": "http",
                "label": "Got it",
                "url": f"{ntfy_server()}/{ack_topic()}",
                "method": "POST",
                "body": "ack",
                "clear": True,
            }
        )
    return title, "\n".join(lines).rstrip(), first["url"], actions


# --------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------

def ntfy_server() -> str:
    return (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")


def ntfy_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("NTFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def ack_topic() -> str:
    """The topic a "stop shouting" tap publishes to.

    A separate topic rather than a flag somewhere: the phone can reach it with
    one tap and no auth dance, and the watcher -- which is a fresh container on
    every poll and remembers nothing -- can read it back on the next run.
    """
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    return f"{topic}-ack" if topic else ""


def acknowledged_since(since_iso: str) -> bool:
    """Has anything been published to the ack topic since `since_iso`?

    Fails closed on a network error -- an unreachable ntfy must not read as
    "acknowledged" and silence an alert that was never seen.
    """
    topic = ack_topic()
    if not topic:
        return False
    try:
        since = int(datetime.fromisoformat(since_iso).timestamp())
        raw = http_get(f"{ntfy_server()}/{topic}/json?poll=1&since={since}", ntfy_headers())
    except Exception as exc:
        log(f"  could not read the ack topic ({exc}); treating as not acknowledged")
        return False
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("event") == "message":
            return True
    return False


def notify_ntfy(title: str, body: str, link: str, actions: list | None = None,
                tags: list | None = None) -> bool:
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return False
    # Publish via ntfy's JSON API rather than its header API: urllib encodes
    # headers as latin-1, and the title carries an em dash, which would blow up
    # with UnicodeEncodeError before the request ever left the machine.
    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        "priority": 5,
        "tags": tags or ["clapper", "tickets"],
    }
    if link:
        payload["click"] = link
    if actions:
        payload["actions"] = actions
    http_post(ntfy_server() + "/", json.dumps(payload).encode("utf-8"), ntfy_headers())
    log("  notified: ntfy")
    return True


def notify_webhook(title: str, body: str, link: str) -> bool:
    url = (os.environ.get("WEBHOOK_URL") or "").strip()
    if not url:
        return False
    text = f"**{title}**\n{body}"
    if link:
        text += f"\n{link}"
    host = urllib.parse.urlparse(url).netloc.lower()
    if "slack.com" in host:
        payload = {"text": text}
    elif "discord" in host:
        # Discord rejects bodies over 2000 characters outright.
        payload = {"content": text[:1900]}
    else:
        payload = {"title": title, "text": text, "body": body, "url": link}
    http_post(url, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
    log("  notified: webhook")
    return True


def notify(title: str, body: str, link: str, actions: list | None = None) -> int:
    sent = 0
    for fn in (notify_ntfy, notify_webhook):
        try:
            if fn(title, body, link, actions) if fn is notify_ntfy else fn(title, body, link):
                sent += 1
        except Exception as exc:  # one broken channel must not silence the other
            log(f"  !! {fn.__name__} failed: {exc}")
    if not sent:
        log("  !! no notification channel is configured (set NTFY_TOPIC and/or WEBHOOK_URL)")
    return sent


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"alerted": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Theatre resolution
# --------------------------------------------------------------------------

def resolve_theatres(api: Api, cfg: dict) -> list[tuple[str, str]]:
    """Map the configured theatre name patterns onto live Cineplex ids."""
    wanted = cfg.get("theatres") or []
    pinned = [t for t in wanted if t.get("id")]
    if len(pinned) == len(wanted) and wanted:
        return [(str(t["id"]), t.get("label", str(t["id"]))) for t in wanted]

    catalogue = list_theatres(api, cfg.get("language", "en"))
    log(f"  {len(catalogue)} theatres in the Cineplex catalogue")
    resolved: list[tuple[str, str]] = []
    for spec in wanted:
        if spec.get("id"):
            resolved.append((str(spec["id"]), spec.get("label", str(spec["id"]))))
            continue
        tokens = spec.get("nameContains") or []
        for theatre in catalogue:
            name = field(theatre, "name", "theatreName", "displayName")
            tid = theatre.get("id") or theatre.get("theatreId") or theatre.get("locationId")
            if not name or tid is None:
                continue
            if matches_all(name, tokens):
                pair = (str(tid), name)
                if pair not in resolved:
                    resolved.append(pair)

    if not resolved and catalogue:
        log("  !! no theatre matched the config. A sample of what the API returned:")
        for theatre in catalogue[:25]:
            log(f"       {field(theatre, *NAME_FIELDS)!r}")
        log(f"     (showing 25 of {len(catalogue)})")
    return resolved


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_check(cfg: dict, state_path: Path, dry_run: bool, fixture: Path | None) -> int:
    state = load_state(state_path)
    already = set(state.get("alerted") or [])
    hits: list[dict] = []

    if fixture:
        log(f"offline run against fixture {fixture}")
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        hits = find_matches(payload, str(payload.get("theatreId", "fixture")), "Fixture Theatre", cfg)
    else:
        log("resolving Cineplex API key...")
        api = Api(get_subscription_key())
        log("resolving theatres...")
        theatres = resolve_theatres(api, cfg)
        if not theatres:
            raise RuntimeError(
                "no theatre matched the config -- Cineplex may have renamed a location; "
                "check `theatres[].nameContains` in watch.config.json"
            )
        for tid, name in theatres:
            log(f"  -> {name} (id {tid})")
        for date_iso in cfg.get("targetDates") or []:
            for tid, name in theatres:
                try:
                    payload = get_showtimes(api, tid, date_iso, cfg.get("language", "en"))
                except urllib.error.HTTPError as exc:
                    log(f"  !! {name} {date_iso}: HTTP {exc.code}")
                    continue
                found = find_matches(payload, tid, name, cfg)
                log(f"  {name} {date_iso}: {len(found)} matching session(s)")
                hits.extend(found)

    fresh = sorted(
        (h for h in hits if h["key"] not in already), key=lambda h: (h["date"], h["start"])
    )
    escalating = dict(state.get("escalating") or {})
    # A showtime already announced is still worth re-examining while it is
    # under escalation: the alert repeats until it is acknowledged, and that
    # means re-reading the seat map each poll rather than trusting the reading
    # that started it.
    watching = fresh + [h for h in hits if h["key"] in escalating and h not in fresh]
    log(f"{len(hits)} matching session(s), {len(fresh)} new, {len(escalating)} escalating")

    if not watching:
        write_summary(f"No new showtimes ({len(hits)} already known).")
        return 0

    if not fixture and (cfg.get("seats") or {}).get("partySize"):
        size = cfg["seats"]["partySize"]
        log(f"looking up best {size} adjacent seats for each showtime in play...")
        for hit in watching:
            hit["seatReport"] = best_seats_for(hit["theatreId"], hit.get("session") or {}, cfg)

    return dispatch(watching, fresh, escalating, cfg, state, state_path, already, dry_run)


def dispatch(watching: list[dict], fresh: list[dict], escalating: dict, cfg: dict,
             state: dict, state_path: Path, already: set, dry_run: bool) -> int:
    """Decide which alert this poll owes the user, and record what it sent."""
    spec = cfg.get("escalate") or {}
    window = float(spec.get("maxMinutes", 120))
    # An acknowledgement is permanent for the showtimes it silenced. Clearing
    # only the live escalation would let the next poll -- which still sees the
    # same free seats -- start the whole thing over from zero, which is how a
    # "stop" button turns into a snooze button.
    settled = set(state.get("acknowledged") or [])
    go = [h for h in watching
          if (h.get("seatReport") or {}).get("target") and h["key"] not in settled]

    # One tap silences every showtime, because only one of them gets bought.
    if escalating and not dry_run:
        since = min(entry["since"] for entry in escalating.values())
        if acknowledged_since(since):
            log("  acknowledged on the ack topic -- standing down")
            state["acknowledged"] = sorted(settled | set(escalating))
            state["escalating"] = {}
            state["alerted"] = sorted(already | {h["key"] for h in watching})
            save_state(state_path, state)
            write_summary("Acknowledged; escalation stopped.")
            return 0

    # Seats that vanished mid-escalation: say so once rather than going quiet,
    # because the last thing sent was an instruction to go and buy them.
    lost = [k for k in escalating if k not in {h["key"] for h in go}]
    for key in lost:
        log(f"  target seats gone for {key} -- escalation ends")
        escalating.pop(key)

    if go and all(within_escalation_window(escalating.get(h["key"]), window) for h in go):
        title, body, link, actions = go_message(go, cfg)
        log(f"\n{title}\n{body}\n{link}\n")
        write_summary(f"## {title}\n\n{body}\n\n{link}")
        if dry_run:
            log("dry run -- not sending notifications, not recording state")
            return 0
        notify(title, body, link, actions)
        now = datetime.now(timezone.utc).isoformat()
        for hit in go:
            entry = escalating.get(hit["key"]) or {"since": now, "sent": 0}
            entry["sent"] += 1
            entry["seats"] = format_block(hit["seatReport"]["target"])
            escalating[hit["key"]] = entry
        state["escalating"] = escalating
        state["alerted"] = sorted(already | {h["key"] for h in watching})
        save_state(state_path, state)
        return 0

    if go:
        log(f"  escalation ran its {window:g}-minute course -- standing down")
        settled |= {h["key"] for h in go}
        state["acknowledged"] = sorted(settled)
        escalating = {}

    if not fresh:
        state["escalating"] = escalating
        if lost:
            title = "Target seats gone"
            body = "The seats you were told to take are no longer free. " \
                   "Run a check for what is left."
            log(f"\n{title}\n{body}\n")
            if not dry_run:
                notify(title, body, next((h["url"] for h in watching if h.get("url")), ""))
        if not dry_run:
            save_state(state_path, state)
        write_summary("No new showtimes.")
        return 0

    title, body, link = build_message(fresh, cfg)
    log(f"\n{title}\n{body}\n{link}\n")
    write_summary(f"## {title}\n\n" + "\n".join(f"- {describe(h)}" for h in fresh) + f"\n\n{link}")

    if dry_run:
        log("dry run -- not sending notifications, not recording state")
        return 0

    notify(title, body, link)
    state["escalating"] = escalating
    state["alerted"] = sorted(already | {h["key"] for h in fresh})
    save_state(state_path, state)
    return 0


def rehearsal_setup(cfg: dict, dates: list[str] | None) -> tuple[dict, Path]:
    """Aim a real run at today, so a drill exercises every real path.

    The Odyssey is playing in IMAX 70mm right now, so pointing the watcher at
    today drives matching, the live seat map and the notification for real
    rather than simulating any of them.

    Two safeguards, both of which the tests pin. The ledger is a throwaway in
    a temp directory, so a drill can never mark a genuine 17 Sept showtime as
    already-reported and swallow the alert that matters. And the title says
    DRILL, so the push cannot be mistaken for the real thing.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    out = dict(cfg)
    out["targetDates"] = dates or [today]
    out["label"] = "DRILL (not real) — " + (cfg.get("label") or "Showtimes")
    return out, Path(tempfile.mkdtemp(prefix="rehearsal-")) / "seen.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--date", action="append", help="override targetDates (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="report but do not notify or save state")
    parser.add_argument("--fixture", type=Path, help="parse a local JSON file instead of calling the API")
    parser.add_argument("--test-notify", action="store_true", help="send a test alert and exit")
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="dress rehearsal: run the real thing against today's showtimes",
    )
    parser.add_argument("--probe", action="store_true", help="show what the API actually returns")
    parser.add_argument("--dump", type=Path, help="with --probe, save the raw API responses here")
    args = parser.parse_args()

    if args.test_notify:
        sent = notify(
            "Cineplex watcher test",
            "If you can read this, alerts are wired up correctly.",
            "https://github.com/IdanG7/cineplex",
        )
        return 0 if sent else 1

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if args.date:
        cfg["targetDates"] = args.date

    if args.rehearse:
        cfg, args.state = rehearsal_setup(cfg, args.date)
        log(f"REHEARSAL: treating {cfg['targetDates']} as the target date(s).")
        log("REHEARSAL: notifications ARE sent; the dedup ledger is throwaway.")

    if args.probe:
        return probe(cfg, args.dump)

    return run_check(cfg, args.state, args.dry_run, args.fixture)


def probe(cfg: dict, dump: Path | None = None) -> int:
    """Print what the API actually returns, so a miss can be diagnosed.

    The point of this mode is to distinguish three very different situations
    that all look identical from the outside: the showtimes genuinely are not
    on sale yet, the theatre names no longer resolve, or the response shape
    changed and the matcher is reading fields that are not there any more.
    """
    api = Api(get_subscription_key())
    theatres = resolve_theatres(api, cfg)
    log(f"\nresolved {len(theatres)} theatre(s) from the live catalogue:")
    for tid, name in theatres:
        log(f"  id={tid}  {name}")
    if not theatres:
        log("  !! nothing matched `theatres[].nameContains` in the config")
        return 1

    # Control query. An empty response for 17 Sept could mean the date is not
    # on sale yet, or it could mean the request itself is malformed -- and those
    # look identical from the outside. Asking for a date that certainly does
    # have showtimes separates them.
    #
    # It also does something more valuable: The Odyssey is playing right now, so
    # running the real filter over today's data proves whether the filter can
    # actually recognise a 70mm showing. Without that, a filter that matches
    # nothing is indistinguishable from a date that has nothing -- and the first
    # time we would find out is the day the tickets were missed.
    control_date = datetime.now(timezone.utc).date().isoformat()
    control: dict = {}
    for ctl_id, ctl_name in theatres:
        log(f"\n--- control: today ({control_date}) at {ctl_name} ---")
        payload = get_showtimes(api, ctl_id, control_date, cfg.get("language", "en"))
        control = payload or control
        days = extract_days(payload)
        if not days:
            log(f"  !! CONTROL FAILED here: today returned nothing. shape: {describe_shape(payload)}")
            continue

        films = [
            field(m, "name", "title", "filmName", "movieName")
            for d in days
            for m in (d.get("movies") or [])
        ]
        log(f"  control OK: {len(days)} date block(s), {len(films)} film(s) listed.")

        targets = [
            (d, m)
            for d in days
            for m in (d.get("movies") or [])
            if matches_any(field(m, "name", "title", "filmName", "movieName"), cfg.get("movieMatch") or [])
        ]
        if not targets:
            log(f"  target film not showing here today. Films: {films[:8]}")
            continue

        for _day, movie in targets:
            title = field(movie, "name", "title", "filmName", "movieName")
            log(f"  '{title}' is showing today. Its experiences:")
            for experience in movie.get("experiences") or []:
                types = experience.get("experienceTypes")
                sessions = experience.get("sessions") or []
                blob = " ".join(
                    [
                        flatten_text(types),
                        flatten_text(experience.get("name")),
                        flatten_text(experience.get("experienceName")),
                    ]
                ).strip()
                ok = matches_all(blob or title, cfg.get("formatMatchAll") or []) and matches_any(
                    blob or title, cfg.get("formatMatchAny") or []
                )
                log(
                    f"    {'PASSES' if ok else 'filtered out'}  "
                    f"experienceTypes={json.dumps(types)}  ({len(sessions)} session(s))"
                )
                if sessions:
                    log(f"      session keys: {sorted(sessions[0].keys())}")
                    log(f"      session: {json.dumps(sessions[0])[:1800]}")

        # The real matcher, over real data, for a date that actually has showings.
        live = find_matches(payload, ctl_id, ctl_name, dict(cfg, targetDates=[control_date]))
        log(f"  >> FILTER SELF-TEST: {len(live)} session(s) today would trigger an alert")
        for hit in live[:6]:
            log(f"       {describe(hit)}")
            log(f"       link: {hit['url'] or '(none)'}")

    raw_dump: dict = {"control": control}
    strict_keys: set[str] = set()
    total_sessions = 0

    for tid, name in theatres:
        for date_iso in cfg.get("targetDates") or []:
            log(f"\n=== {name} (id {tid}) — queried date {date_iso} ===")
            payload = get_showtimes(api, tid, date_iso, cfg.get("language", "en"))
            raw_dump[f"{tid}@{date_iso}"] = payload

            log(f"  response shape: {describe_shape(payload)}")
            days = extract_days(payload)
            if not days:
                log("  !! no per-date blocks found in the response")
                log("  !! raw (truncated): " + json.dumps(payload)[:1500])
                continue
            log(f"  {len(days)} date block(s): "
                f"{[date_prefix(d.get('startDate') or d.get('date')) for d in days]}")

            for hit in find_matches(payload, tid, name, cfg):
                strict_keys.add(hit["key"])

            for day in days:
                day_iso = date_prefix(day.get("startDate") or day.get("date"))
                movies = day.get("movies") or []
                marker = ">>" if day_iso in (cfg.get("targetDates") or []) else "  "
                log(f"  {marker} {day_iso}: {len(movies)} film(s)")
                if day_iso not in (cfg.get("targetDates") or []):
                    continue
                for movie in movies:
                    title = field(movie, "name", "title", "filmName", "movieName")
                    if not title:
                        log(f"       !! film with no recognisable title; keys={sorted(movie.keys())}")
                        continue
                    on_target = matches_any(title, cfg.get("movieMatch") or [])
                    log(f"       {'*' if on_target else ' '} {title}")
                    if not on_target:
                        continue
                    for experience in movie.get("experiences") or []:
                        fmt = flatten_text(experience.get("experienceTypes")) or "(no experienceTypes)"
                        sessions = experience.get("sessions") or []
                        total_sessions += len(sessions)
                        log(f"           format: {fmt!r} -> {len(sessions)} session(s)")
                        for session in sessions:
                            key = session_key(tid, day_iso, session)
                            log(
                                f"             {'MATCH' if key in strict_keys else '     '} "
                                f"{field(session, 'showStartDateTime') or '(no showStartDateTime)'}"
                                f"  aud={field(session, 'auditorium') or '?'}"
                                f"  seats={session.get('seatsRemaining', '?')}"
                                f"  soldOut={session.get('isSoldOut', '?')}"
                                f"  bookable={session.get('isShowtimeEnabledOnline', '?')}"
                            )
                            log(f"               session keys: {sorted(session.keys())}")

    log(
        f"\nSUMMARY: {total_sessions} session(s) listed for the target film on the "
        f"target date(s); {len(strict_keys)} pass the configured format filter."
    )
    if total_sessions and not strict_keys:
        log("  -> the film is listed but nothing matched the format filter."
            " Compare the 'format:' lines above against formatMatchAll/formatMatchAny.")
    elif not total_sessions:
        log("  -> the film is not listed for that date yet. This is the expected"
            " state until Cineplex opens the date.")

    if dump:
        dump.write_text(json.dumps(raw_dump, indent=2)[:5_000_000], encoding="utf-8")
        log(f"\nraw API responses written to {dump}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"FATAL: {exc}")
        sys.exit(1)
