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

### Reading the real seat map

A Cineplex auditorium does not come back as one grid. `seat-layout` splits it
into areas — `standardSeats`, `dboxSeats`, `balconySeats` — each carrying its
own rows plus a `left`, `top` and `columnWidth` that place it in one shared
house frame, and `totalRows`/`totalColumns` give that frame its size. Three
things follow, and all three matter for which seats you are told to take:

- **Every area is read, not just the first.** In the Square One UltraAVX house,
  row H is four stub seats by the walls in `standardSeats` and ten D-BOX seats
  filling the centre in `dboxSeats` — same physical row, two areas. Reading one
  area hides 28 seats and leaves the ranker convinced row H seats four people.
- **How far back a row sits comes from `top` + `number`,** not from its position
  in a list. D-BOX row H is row 0 of its own area while sitting fourteen rows
  back in the house.
- **Centring is measured from the screen, not from the row.** Rows are ragged:
  row A of the Square One IMAX runs columns 3–22 in a house 0–28, so a block
  centred in its *own* row sits a seat and a half left of the screen. Using the
  house centre also makes one row's centre penalty mean the same as another's,
  which is the only reason ranking rows against each other means anything.

A block in a premium area is labelled as such — `D-BOX · ideal` — because a
D-BOX seat carries an upcharge and moves during the film, and recommending six
of them without saying so is the same failure as recommending the front row
without saying it is the front row.

`tests/fixtures/seat_layout_*.json` are verbatim captures of both an IMAX 70mm
house and an UltraAVX house with a D-BOX block, so the ranker is tested against
the real response rather than an idea of it.

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

## The buy-now alert

Most of the time a showtime opening is news you read when you wake up. Once,
it is news you have to act on — the moment 17 Sept goes on sale with F/G 12–17
still free. That case gets its own alert.

```jsonc
"escalate": {
  "rows": ["F", "G"],   // the rows worth waking up for
  "seats": [11, 19],    // a block must sit entirely inside this span
  "maxMinutes": 120     // how long it may keep shouting before standing down
}
```

When a ranked block clears that bar, the watcher sends a differently-shaped
notification: title `BUY NOW — F12-F17 open for Thu 17 Sep`, the one showtime
to act on first, and a **Buy now** button that opens that showtime's seat
picker directly. It repeats every poll until you answer.

The seat span is deliberately wider than the party. With `[11, 19]` a party of
six qualifies at 12–17, 13–18 or 11–16 — a rule narrow enough to name one exact
block goes silent the moment a single seat at its edge is taken, which is the
opposite of the point.

**Stopping it.** The notification carries a **Got it** button that publishes to
`$NTFY_TOPIC-ack`. The watcher reads that topic on its next poll and stands
down. One tap silences every showtime that was shouting, because you are only
going to buy one of them. The acknowledgement is permanent for those showtimes
— without that, the next poll sees the same free seats and starts over, which
turns a stop button into a snooze button. A showtime Cineplex adds afterwards
is news again and can escalate on its own.

It stands down on its own in three other cases: the seats stop being free (you
get one plain message saying so, rather than silence after being told to go and
buy them), `maxMinutes` elapses, or ntfy is unreachable — which counts as *not*
acknowledged, so an unreadable ack topic can never silence an alert nobody saw.

`maxMinutes` is wall-clock time rather than a count of polls on purpose. The
polling rate is configured at cron-job.org, where nothing in this repo can see
it change, so a repeat count would mean two hours at one cadence and twenty
minutes at another without anything here looking different.

### What it deliberately does not do

It does not add anything to a cart and it does not buy anything. Nothing here
touches your Cineplex login or your card, and no seat is held for you — you tap
through to the seat picker and complete the purchase yourself.

That is a deliberate limit, not a missing feature:

- Movie tickets are non-refundable, and an auto-buyer that misfires buys six
  seats at the wrong theatre, the wrong showtime, or in row A.
- Cineplex's terms prohibit automated purchasing. The realistic downside is a
  cancelled order or a locked account, on precisely the showing you were trying
  to protect.

The scarce thing is the seats, not the checkout. Going from notification to
seat picker in one tap is where the time is actually won; your phone fills in
the payment in the seconds after that.

### One thing to know about the ack topic

`$NTFY_TOPIC-ack` is a public ntfy topic, like the main one. Anyone who knows
the name can publish to it and silence an escalation. The topic name is the
only secret, so keep it out of screenshots — and note that a repository
*variable* is not masked in workflow logs, while a *secret* is.

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
- **GitHub's scheduler is unreliable at high frequency.** `*/5` produced one
  run in 75 minutes here; the schedule is now every 15 minutes at off-boundary
  minutes, which the scheduler is far likelier to honour. For 5-minute
  resolution use the external cron in [SETUP-CRON.md](SETUP-CRON.md).
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
