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
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
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

def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    raw = http_get(url, {"Accept": "application/json", **(headers or {})}, timeout)
    return json.loads(raw.decode("utf-8"))


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
    override = (os.environ.get("CINEPLEX_API_KEY") or "").strip()
    if override:
        log("  using CINEPLEX_API_KEY from the environment")
        return override
    return discover_subscription_key()


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


def list_theatres(api: Api, language: str = "en") -> list[dict]:
    payload = api.get("theatres", {"language": language})
    if isinstance(payload, list):
        return payload
    for field in ("items", "theatres", "data", "results"):
        value = payload.get(field)
        if isinstance(value, list):
            return value
    return []


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


def iter_sessions(payload: dict, target_dates: list[str]):
    """Yield (date, movie, experience, session) for the dates we care about."""
    for day in payload.get("dates") or []:
        day_iso = date_prefix(day.get("startDate") or day.get("date"))
        if target_dates and day_iso not in target_dates:
            continue
        for movie in day.get("movies") or []:
            for experience in movie.get("experiences") or []:
                for session in experience.get("sessions") or []:
                    yield day_iso, movie, experience, session


def session_key(theatre_id: str, date_iso: str, session: dict) -> str:
    vista = field(session, "vistaSessionId", "sessionId", "id")
    if vista:
        return f"{theatre_id}:{vista}"
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
                "url": field(session, "deeplinkUrl", "seatMapUrl"),
            }
        )
    return hits


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

def pretty_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        return value or "time TBA"


def describe(hit: dict) -> str:
    bits = [pretty_time(hit["start"]), hit["theatre"]]
    if hit.get("auditorium"):
        bits.append(f"aud {hit['auditorium']}")
    if hit.get("isSoldOut"):
        bits.append("SOLD OUT")
    elif isinstance(hit.get("seatsRemaining"), int):
        bits.append(f"{hit['seatsRemaining']} seats left")
    return " · ".join(bits)


def build_message(hits: list[dict], cfg: dict) -> tuple[str, str, str]:
    label = cfg.get("label") or "Showtimes"
    title = f"{label} — {len(hits)} new showtime{'s' if len(hits) != 1 else ''}"
    lines = [describe(h) for h in hits]
    link = next((h["url"] for h in hits if h.get("url")), cfg.get("fallbackUrl", HOMEPAGE))
    return title, "\n".join(lines), link


# --------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------

def notify_ntfy(title: str, body: str, link: str) -> bool:
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return False
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    # Publish via ntfy's JSON API rather than its header API: urllib encodes
    # headers as latin-1, and the title carries an em dash, which would blow up
    # with UnicodeEncodeError before the request ever left the machine.
    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        "priority": 5,
        "tags": ["clapper", "tickets"],
    }
    if link:
        payload["click"] = link
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("NTFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    http_post(server + "/", json.dumps(payload).encode("utf-8"), headers)
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


def notify(title: str, body: str, link: str) -> int:
    sent = 0
    for fn in (notify_ntfy, notify_webhook):
        try:
            if fn(title, body, link):
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
    log(f"{len(hits)} matching session(s), {len(fresh)} new")

    if not fresh:
        write_summary(f"No new showtimes ({len(hits)} already known).")
        return 0

    title, body, link = build_message(fresh, cfg)
    log(f"\n{title}\n{body}\n{link}\n")
    write_summary(f"## {title}\n\n" + "\n".join(f"- {describe(h)}" for h in fresh) + f"\n\n{link}")

    if dry_run:
        log("dry run -- not sending notifications, not recording state")
        return 0

    notify(title, body, link)
    state["alerted"] = sorted(already | {h["key"] for h in fresh})
    save_state(state_path, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--date", action="append", help="override targetDates (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="report but do not notify or save state")
    parser.add_argument("--fixture", type=Path, help="parse a local JSON file instead of calling the API")
    parser.add_argument("--test-notify", action="store_true", help="send a test alert and exit")
    parser.add_argument("--probe", action="store_true", help="dump what is listed, to verify config")
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

    if args.probe:
        return probe(cfg)

    return run_check(cfg, args.state, args.dry_run, args.fixture)


def probe(cfg: dict) -> int:
    """Show every session for the target film at the configured theatres.

    Format and date filters are dropped so a miss can be told apart from a
    config that is quietly matching nothing.
    """
    api = Api(get_subscription_key())
    theatres = resolve_theatres(api, cfg)
    log(f"resolved {len(theatres)} theatre(s)")
    loose = dict(cfg, formatMatchAll=[], formatMatchAny=[], targetDates=[], requireBookable=False)
    for tid, name in theatres:
        log(f"\n=== {name} (id {tid}) ===")
        for date_iso in cfg.get("targetDates") or []:
            payload = get_showtimes(api, tid, date_iso, cfg.get("language", "en"))
            days = [date_prefix(d.get("startDate") or d.get("date")) for d in payload.get("dates") or []]
            log(f"  query date {date_iso} -> dates returned: {days or 'none'}")
            for hit in find_matches(payload, tid, name, loose):
                flag = "MATCH" if find_matches(payload, tid, name, cfg) else "     "
                log(f"  {flag} {hit['date']} {hit['format']:<28} {describe(hit)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"FATAL: {exc}")
        sys.exit(1)
