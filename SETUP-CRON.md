# Driving the watcher on a real schedule

GitHub's own `schedule` trigger is best-effort, and on this repo it has been
poor: one automatic run in the first 75 minutes against a `*/5` schedule. That
is fine for a nightly report and not fine for catching a ticket drop.

## Why GitHub's scheduler behaves this way

Worth knowing, because it decides how much to trust it.

- **Five minutes is the floor.** `*/5` is the shortest interval GitHub accepts;
  anything more frequent is silently rejected rather than rounded.
- **`schedule` is best-effort, and runs are dropped, not just delayed.** The
  trigger sits in a queue GitHub drains on a best-effort basis. Under load it
  slips — and when the queue is saturated it can skip a firing outright,
  leaving no trace of the missed run. That is exactly what we observed: no
  failed runs, no cancelled runs, simply nothing where a run should have been.
- **The delay is upstream of your runner.** It is in GitHub's event dispatch,
  before a job is ever queued, so nothing about the workflow itself can fix it.
- **`:00` is the worst minute on the platform.** GitHub's own documentation
  says to avoid the start of an hour. Community reports put normal delays at
  5–30 minutes there, and over 60 minutes during peak windows. Reports
  disagree on whether `:30` is bad or fine; `:15`, `:30` and `:45` are all
  busier than an arbitrary minute, so the safe move is to avoid all four
  quarter-hours. Minutes like 17, 23 and 41 are reported as quiet, because
  almost nobody picks them.
- **Manual and API triggers are not affected.** `workflow_dispatch` and
  `repository_dispatch` are picked up near-instantly. It is specifically the
  `schedule` event that gets deprioritised — which is the whole reason the
  setup below works.

This repo's schedule is now `7,22,37,52 * * * *`: every 15 minutes, sitting as
far from `:00`, `:15`, `:30` and `:45` as it is possible to sit. That makes it a
usable backstop. It does not make it something to depend on for a ticket drop,
which is what the external cron below is for.

The workflow already accepts `repository_dispatch`, so any external cron can
drive it on a schedule that actually holds. Setup is about five minutes.

## 1. Create a token

**github.com → Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**

| Field | Value |
| --- | --- |
| Token name | `cineplex-watcher-cron` |
| Expiration | 90 days (must outlast 17 Sept — put a reminder to rotate it) |
| Repository access | **Only select repositories** → `IdanG7/cineplex` |
| Permissions → Repository → **Contents** | **Read and write** |

Contents write is what the dispatches endpoint requires for fine-grained
tokens. Nothing else needs enabling. If the test in step 2 returns 403, add
**Actions: Read and write** and try again.

Copy the token now — GitHub shows it once. Treat it like a password: it can
write to this repository.

## 2. Test it before automating anything

Run this once from any terminal, substituting your token:

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/IdanG7/cineplex/dispatches \
  -d '{"event_type":"poll"}'
```

**`204 No Content` is success** — there is no response body, and a new run
appears in the Actions tab within a few seconds, labelled with the
`repository_dispatch` event.

- `401` → the token is wrong or was copied with whitespace.
- `403` → the token lacks Contents write, or was not scoped to this repo.
- `404` → almost always the same thing as 403; GitHub hides repos a token
  cannot see. Re-check **Repository access**.

Do not move on until you get a 204 and see the run.

## 3. Point a cron service at it

Any scheduler works. [cron-job.org](https://cron-job.org) is free and needs no
card.

**Create cronjob →**

| Field | Value |
| --- | --- |
| Title | `Cineplex Odyssey watcher` |
| URL | `https://api.github.com/repos/IdanG7/cineplex/dispatches` |
| Schedule | Every 1 minute |
| Request method | **POST** |

Then open **Advanced / Headers** and add:

```
Accept: application/vnd.github+json
Authorization: Bearer YOUR_TOKEN_HERE
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Request body:

```json
{"event_type":"poll"}
```

Turn on failure notifications if the service offers them — a cron that quietly
stops is the same as no cron.

## 4. Confirm it is really running

Wait ten minutes, then look at the Actions tab. You want runs labelled
`repository_dispatch`, roughly five minutes apart. Two or three in a row is
enough to trust it.

Once that holds you can ignore GitHub's own schedule entirely. Leave the
`schedule:` trigger in place — it costs nothing and is a free second chance if
the cron service has an outage. The `concurrency` group in the workflow means
overlapping triggers queue rather than run on top of each other, and the
`state/seen.json` ledger means a showtime is only ever announced once no
matter how many triggers fire.

## Cost

The repository is public, so Actions minutes are free and unlimited. At every
minute that is ~1,440 runs a day, each about ten seconds — roughly two seconds
of watcher plus checkout and the state commit.

## What a one-minute cadence changes

Polling every minute is what actually decides whether you get the seats: the
watcher itself runs in about two seconds, so essentially all of the delay
between tickets opening and your phone buzzing is the gap between polls. Going
from five minutes to one cuts the average delay from ~150 seconds to ~30.

Three things in this repo are set up for that rate specifically:

- **`timeout-minutes: 3` on the job.** The `concurrency` group keeps at most
  one run pending, so a hung run blocks every poll queued behind it. A short
  timeout caps that at three missed polls instead of ten.
- **No `setup-python` step.** `ubuntu-latest` already ships 3.12 and the
  watcher has no dependencies; provisioning a second copy spent 10–20 seconds
  of a 60-second budget to change nothing.
- **`escalate.maxMinutes`, not a repeat count.** The buy-now alert repeats for
  a number of *minutes*, because the polling rate lives at cron-job.org where
  nothing in this repo can see it change. A count of repeats would silently
  mean two hours at one cadence and twenty-four minutes at another.

## Two settings that pay for themselves

Both are optional and both make each run faster and less breakable.

**Pin the theatre ids.** `watch.config.json` now carries `"id": "7420"` and
`"id": "7408"`, which skips fetching and name-matching the 152-theatre
catalogue on every poll. Name matching still runs if you remove the ids.

**Optionally set `CINEPLEX_API_KEY` as a repository secret.** This is
insurance, not speed — do not set it expecting the watcher to get faster.

Every run scrapes the key out of Cineplex's JS bundles, which costs about a
second and is self-correcting: rotate the key on their side and the next run
simply picks up the new one. What scraping cannot survive is Cineplex
restructuring those bundles, which would take the watcher dark with no warning.
`CINEPLEX_API_KEY` is the floor under that case — it is consulted *only* when
scraping fails, so a stale value costs nothing and a working one keeps the
watch alive while you fix the scraper.

Print your own copy locally:

```bash
python3 -c "import cineplex_watch; print(cineplex_watch.discover_subscription_key())"
```

Put that value in **Settings → Secrets and variables → Actions → Secrets** as
`CINEPLEX_API_KEY`. Use a **secret**, not a variable — variables are not masked
in workflow logs and this repository is public.

A rotation is handled twice over: scraping picks up the new key on the next
run, and a rejected key is re-derived mid-run on any 401 or 403. The secret is
never the thing in charge, so it cannot go stale in a way that costs you a
showtime.

## Turning it off

Delete or pause the cronjob, then delete the token at
**Settings → Developer settings → Fine-grained tokens**. Do the token too —
a scheduler you have forgotten about holding a live write token is exactly the
thing that goes wrong six months later.
