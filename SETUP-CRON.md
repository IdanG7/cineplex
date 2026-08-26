# Driving the watcher on a real schedule

GitHub's own `schedule` trigger is best-effort, and on this repo it has been
poor: one automatic run in the first 75 minutes against a `*/5` schedule. That
is fine for a nightly report and not fine for catching a ticket drop.

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
five minutes that is ~288 runs a day, each about 15 seconds.

## Turning it off

Delete or pause the cronjob, then delete the token at
**Settings → Developer settings → Fine-grained tokens**. Do the token too —
a scheduler you have forgotten about holding a live write token is exactly the
thing that goes wrong six months later.
