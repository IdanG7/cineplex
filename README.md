# Cineplex ticket watcher

Watches Cineplex for **The Odyssey in IMAX 70mm on Thu 17 Sept 2026** at the two
GTA houses that can project it, and pushes you an alert the moment a showtime
becomes bookable.

Cineplex releases 70mm dates in waves rather than all at once, so the thing worth
catching is the moment a date flips from "nothing listed" to "sessions exist".
The watcher polls every ~5 minutes from GitHub Actions and alerts only on
showtimes it has not already told you about — so later waves for the same date
still reach you, but you never get the same alert twice.

## Activation

GitHub only runs `schedule` triggers from a repository's **default branch**, so
the workflow has to live there. `main` carries it.

If you change the default branch, make sure the workflow moves with it or the
schedule stops firing. `workflow_dispatch` has the same rule, so manual runs
need it there too.

## Scheduling

GitHub's own `schedule` trigger is best-effort and has been unreliable here —
one automatic run in the first 75 minutes against a `*/5` schedule. Good enough
for a nightly report, not good enough for catching a ticket drop.

**[SETUP-CRON.md](SETUP-CRON.md) sets up an external cron** that drives the
workflow through `repository_dispatch` on a schedule that actually holds. About
five minutes, needs a fine-grained token. Leave the `schedule:` trigger in place
alongside it — it costs nothing and is a free second chance.

## Setup (one time, ~2 minutes)

The watcher runs on its own. It just needs somewhere to send the alert.
Add these under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Needed? | What it is |
| --- | --- | --- |
| `NTFY_TOPIC` | for phone push | A topic name you invent, e.g. `odyssey-70mm-a7f3q2`. Install the [ntfy](https://ntfy.sh) app, subscribe to the same topic. |
| `WEBHOOK_URL` | for Discord/Slack | An incoming webhook URL. Discord and Slack are detected automatically. |
| `NTFY_SERVER` | optional | Self-hosted ntfy. Defaults to `https://ntfy.sh`. |
| `NTFY_TOKEN` | optional | Bearer token, if your ntfy topic is access-controlled. |
| `CINEPLEX_API_KEY` | optional | Pin the API key instead of rediscovering it each run. |

The workflow reads each of these from a **secret or a repository variable**, so
either tab works. Secrets take precedence when both are set.

Prefer secrets anyway. Repository variables are printed in plain text in workflow
logs, and this repo is public — so a variable is world-readable the moment
anything echoes it. Secrets are masked automatically. `NTFY_TOKEN` is
secret-only for that reason.

> Pick an unguessable `NTFY_TOPIC`. On ntfy.sh the topic name *is* the
> credential: anyone who knows it can read your alerts and publish fake ones to
> your phone. A random suffix costs nothing — `odyssey-70mm-a7f3q2`, not
> `bps-ody`.

Then confirm it works: **Actions → Watch Odyssey tickets → Run workflow →
`test-notify`**. You should get a push within seconds. If nothing arrives, the
run log will say which channel failed and why.

## Proving it actually works

The fastest way to confirm the watcher reads real theatres, showtimes and seat
counts is to run the probe yourself on any machine with internet:

```bash
git clone https://github.com/IdanG7/cineplex && cd cineplex
python3 cineplex_watch.py --probe
```

No dependencies, no secrets, nothing is sent anywhere. It prints the theatre ids
it resolved from Cineplex's live catalogue, every film listed on the target date,
the exact format string on each showing, and the seat count and booking status of
every session — marking which ones the filter would alert on. It ends with a
plain summary that separates the three cases that otherwise look identical:

- *the film is not listed for that date yet* — expected, until Cineplex opens it
- *listed, but nothing matched the format filter* — the filter needs adjusting
- *matched N sessions* — the watcher is working

Add `--dump raw.json` to save the untouched API responses.

## Checking on it

From the **Actions** tab, `Run workflow` gives you four modes:

- **`check`** — what the schedule runs: find new showtimes, alert, record them.
- **`probe`** — lists *every* Odyssey session at the configured theatres with the
  date and format filters switched off. This is the one to run if you suspect
  it is quietly matching nothing.
- **`dry-run`** — a real check that reports but neither alerts nor records.
- **`test-notify`** — sends a test alert to confirm delivery.
- **`rehearse`** — a full dress rehearsal on real data. See below.

## Dress rehearsal

`rehearse` answers "what will actually happen when 17 Sept opens?" without
waiting for it. The Odyssey is playing in IMAX 70mm right now, so the mode
points the watcher at *today* and lets every real path run: it matches live
sessions, pulls the real seat map, ranks the best 5 adjacent seats, and pushes
a genuine notification to your phone. Nothing is simulated.

Two safeguards make it safe to run any time:

- The dedup ledger is a throwaway in a temp directory, so a drill can never
  mark a genuine 17 Sept showtime as already-reported and swallow the alert
  you are waiting for.
- The title is prefixed **DRILL (not real)**, so it cannot be mistaken for the
  real thing.

The filter, the theatres and the seat preferences are untouched — a drill that
quietly widened the filter would prove nothing.


## Which seats it picks

The alert does not just say a showtime opened — it says which seats to take.
When a matching showtime appears the watcher pulls the live seat map from
Cineplex's ticketing API (no auth needed) and ranks every run of 6 adjacent
seats, best first.

The ranking comes from how IMAX 1.43:1 is meant to be watched. The Odyssey is
shot entirely in that ratio, so the frame is floor-to-ceiling tall:

- **About two thirds back.** Far enough that the full height of the frame sits
  inside your field of view without moving your head. Nolan's own advice is to
  sit "a little behind the centre line"; the wider consensus for a ~15-row IMAX
  house is rows 8–11. Too close and the image spills past your peripheral
  vision and strains your neck; too far back and it stops enveloping you.
- **Dead centre horizontally.** Centres you in the 12-channel audio field and
  avoids the keystoning you get from the sides of a curved screen.

The alert never sells a bad seat as a good one. Each pick carries a plain
verdict — `ideal`, `a bit close`, `front row`, `very back`, `good row,
off-centre` — because the ranker returns the best block that *exists*, and on
a picked-over showing that can be row A. It also distinguishes "no 6 can sit
together here (longest run 4)" from "the seat map could not be read", which
otherwise look identical.

Two rules it will not break: a block never straddles an aisle (a gap in the
column numbering), and accessible and companion seats are never recommended.

Tune it in `watch.config.json`:

```jsonc
"seats": {
  "partySize": 6,
  "targetRowFraction": 0.65,   // 0 = front row, 1 = back row
  "rowWeight": 1.0,            // how much row position matters
  "centerWeight": 0.8,         // ...versus being centred
  "avoidSeatTypes": ["wheelchair", "companion"],
  "topN": 3                    // how many options to name in the alert
}
```

If the seat map cannot be read, the alert still goes out — it just falls back
to telling you to pick 6 together, centre, two thirds back. Losing the alert
because the seat API hiccuped would be the worst possible failure.

## Changing what it watches

Everything lives in [`watch.config.json`](watch.config.json):

```jsonc
{
  "targetDates": ["2026-09-17"],          // add more dates freely
  "movieMatch": ["odyssey"],              // any of these in the film title
  "formatMatchAll": ["imax"],             // all of these must be present
  "formatMatchAny": ["70mm", "70 mm"],    // at least one of these
  "requireBookable": true,                // skip sessions not yet on sale
  "theatres": [
    { "nameContains": ["mississauga"], "label": "..." },
    { "nameContains": ["vaughan"],     "label": "..." }
  ]
}
```

Matching ignores case, spaces and punctuation, so `70mm`, `70 mm` and `70-MM`
are the same string. Theatres are resolved by name against the live Cineplex
catalogue on every run, so a renamed or re-IDed location does not silently
break the watch — it fails the run loudly instead. You can pin an exact id with
`{"id": "1234", "label": "..."}` if you prefer.

To widen to any format, set `formatMatchAll` and `formatMatchAny` to `[]`.
To watch the other Canadian 70mm houses, add entries for `langley`,
`riverport`, `chinook`, `edmonton`, `banque scotia`, or `bayers lake`.

## How it works

```
GitHub Actions (every ~5 min)
  └─ scrape the API key out of Cineplex's own JS bundle
  └─ GET /theatres            → resolve theatre names to ids
  └─ GET /showtimes           → per theatre, per target date
  └─ filter: film · date · format · actually bookable
  └─ diff against state/seen.json
  └─ new ones → ntfy push + webhook, then record them
```

The API is `apis.cineplex.com/prod/cpx/theatrical/api/v1`, the same one
cineplex.com's own front end calls. It needs an `Ocp-Apim-Subscription-Key`
header whose value the site ships to every browser; the watcher re-derives it
from the current JS bundle on each run, so a key rotation on Cineplex's side
heals itself rather than silently breaking the watch.

`state/seen.json` is the dedup ledger. The workflow commits it back to this
branch, but only when it changes — so a commit appearing there means real new
showtimes were found.

## Things worth knowing

- **GitHub disables scheduled workflows after 60 days of repo inactivity.** The
  state commits count as activity, but if nothing is ever found, re-run the
  workflow manually once in a while. For a 17 Sept target this is not a concern.
- **Alerts go only where you point them.** Until `NTFY_TOPIC` or `WEBHOOK_URL`
  is set, a run that finds showtimes logs them to the job summary and says no
  channel is configured. It does not fail, so set at least one secret.
- **`*/5` is a request, not a promise.** GitHub delays scheduled runs under
  load; 5–15 minutes is realistic.
- **A failed run emails you** (GitHub does this by default for your own repos),
  which is the backstop for the watcher breaking rather than the tickets never
  appearing.
- **Nothing here holds or buys a seat.** It tells you a showtime exists and
  hands you the direct booking link; you still have to be quick.

## Development

```bash
python3 -m unittest discover -s tests -v          # offline tests, no network
python3 cineplex_watch.py --fixture tests/fixtures/showtimes_sample.json --dry-run
python3 cineplex_watch.py --probe                 # needs network access to Cineplex
```

Standard library only — no dependencies, no install step.
