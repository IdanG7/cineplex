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
| Schedule | Every 5 minutes |
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
five minutes that is ~288 runs a day, each about fifteen seconds — roughly two
seconds of watcher plus checkout and the state commit.

## Do not go below five minutes on GitHub Actions

This was tried and it broke. Five minutes ran 86 consecutive green runs at
13–27 seconds each; switching the cron to one minute produced, within half an
hour, four outright failures, four `startup_failure`s, three cancellations and
three runs stuck in the queue for over twenty minutes. The annotation on the
failures says it plainly:

> The job was not acquired by Runner of type hosted even after multiple attempts

The watcher is not the bottleneck — it finishes in about two seconds. The cost
is the *runner allocation*: a one-minute cadence asks GitHub for ~1,440 hosted
runners a day and the free pool throttles it. Nothing in this repository can
fix that, because the failure happens before any step of the job runs.

Note that `timeout-minutes` does not help here. It governs a job that is
running too long, not one that never acquired a runner, which is why those
failures sat for fifteen minutes with a three-minute timeout configured.

**If you genuinely need sub-minute detection, GitHub Actions is the wrong
vehicle.** A poll is a two-second Python call with no dependencies; run it from
a machine that is already on — a laptop, a Raspberry Pi, a cheap VPS — in a
`while true; do python3 cineplex_watch.py; sleep 30; done` loop, and there is
no runner to allocate at all. Actions is the right home for a five-minute
watch, not a thirty-second one.

## What the five-minute cadence relies on

The watcher runs in about two seconds, so essentially all of the delay between
tickets opening and your phone buzzing is the gap between polls: ~150 seconds
on average at five minutes.

Three things in this repo are set up for that:

- **`cancel-in-progress: true`.** A poller wants the freshest reading, never a
  backlog of stale ones. Queueing meant one run that could not get a runner
  blocked every poll behind it; superseding costs one poll instead.
- **`timeout-minutes: 3` on the job.** Caps the damage from a job that hangs
  once it *is* running. (It does nothing for runner acquisition — see above.)
- **No `setup-python` step.** `ubuntu-latest` already ships 3.12 and the
  watcher has no dependencies; provisioning a second copy spent 10–20 seconds
  to change nothing.
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
