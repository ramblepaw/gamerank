# GameRank

A self-hosted tracker for a large DRM-free PC game library. It answers two
questions a spreadsheet is bad at: *which games have actually been checked to
see if they run*, and *which ones should be removed to keep the library under a
size limit*.

Built to sit alongside a game library server, but it doesn't talk to one — it
keeps its own SQLite database and writes a plain CSV alongside it, so the data
outlives the app.

## What it does

**Verify** — a random slate of unchecked games. Works or it doesn't. Broken goes
to a triage list with a note.

**Grade** — a random slate of checked games. S/A/B/C/D, minutes played, keep or
remove, notes. One grade per game is final, so two people never grade the same
thing twice.

**Broken** — triage, not a delete queue: unaddressed, investigating, fixed (back
to verify), unfixable.

**Removal** — low grades, remove flags and unfixable entries, worst first.
Updates the tracker only; deleting files is left to you.

**Add** — paste a markdown list of the form `* [Title](store-url)`. Store IDs are
read out of the URLs. Titles already tracked get their link attached rather than
duplicated, and near-matches are flagged for confirmation instead of merged.

**Wishlist** — games not yet acquired. No slot cost until one moves across.

**Recent** — the last N games touched, with cover art and a plain-text export.

## The slot economy

The point is to stop an unchecked backlog growing forever.

The balance starts at 50. Adding a game spends one; every two checks returns
one, capped at the limit with no banking. A check earns credit whether the game
worked or not — the credit is for shrinking the unchecked pile, not for the
outcome.

Both numbers are configurable. *Checks per slot* is the dial: at 2 the unchecked
pile shrinks by one for every two checked. Drop it to 1 once the backlog is gone
and you only want to hold the line.

## Metadata

Cover art, store links and release dates come from IGDB. Its `websites` and
`external_games` fields already carry store links, so nothing else is queried.

Matching is deliberately conservative, in this order:

1. Exact name (case- and accent-insensitive)
2. The part before a subtitle — `Shapez 2` matches `Shapez 2: Factory`
3. A single edit distance, requiring the first letter and any sequence numbers
   to agree

That last rule is what makes misspellings recoverable: `Snalland` finds
*Smalland*, `Athermancer` finds *Aethermancer*. The guards matter as much as the
matching — `Fallout 3` is also one edit from `Fallout 4`, so numbers must agree;
`portal` is one edit from `mortal`, so the first letter must agree. Candidates
are ranked rather than taken first-past-the-post, so a franchise search doesn't
attach the VR or DLC entry.

Anything less certain is left for a person on the **Needs art** page, which
offers IGDB results as a cover grid with a search box. Wrong art across a large
library is worse than no art.

Optionally, a run can correct spellings in your own titles from the matched
name. It only fires on genuine typo matches, never rewrites a plain title into a
subtitled one, and logs the old spelling so it can be undone.

Set credentials from https://dev.twitch.tv/console/apps:

```
TWITCH_CLIENT_ID=your-id
TWITCH_CLIENT_SECRET=your-secret
```

Admin has a connection test and a background fetch that walks the whole library
with progress and a stop button.

## Running it locally

```powershell
.\run-local.ps1
```

Serves on http://localhost:8099 with data in `.\data\`. Sign in as **Admin** with
no password. Delete the `data` folder to start over.

## Running it with Docker

Create a `.env` next to `docker-compose.yml` (see `.env.example`) with
`GRT_SECRET` and, if you want cover art, the two Twitch values. Then:

```bash
docker compose up -d
```

Edit `docker-compose.yml` first to point the volume at wherever you want the
database and CSV exports kept, and to set the published port and the `user:`
line to whatever suits your host. The image is published to GitHub Container
Registry by the workflow in `.github/workflows/docker.yml` on every push to
`main`; `docker-compose.build.yml` builds from source instead.

It makes no outbound connections other than to IGDB, and expects to sit behind
whatever reverse proxy or network policy you already run.

## First run

1. Sign in as **Admin**, no password.
2. Admin → **Import**, upload a CSV export of your existing list.
3. Admin → **Users**, add anyone else who'll be checking games.
4. Admin → **Your password**, set one.

The importer expects a `Title` column and tolerates a legend block above the
header and section marker rows, since that's what a hand-maintained sheet
usually looks like. Malformed dates are repaired, undated rows get a
configurable fallback, and duplicate titles are reported.

## Backups

A CSV is written to `/data/exports/` after every change — `masterlist-current.csv`
plus a timestamped snapshot, last 60 kept. That directory is a host mount, so the
hardcopy is a plain file whether or not the container runs.

The export preserves the original column order and section layout, including
grouping headers that hold no rows directly, and round-trips: exporting and
re-importing returns what you started with.

## Text export

`/export.txt` produces a plain list. `fmt` is `markdown`, `title_appid`, `titles`
or `urls`; `scope` is `recent`, `all`, `section` or `removal`; `n` caps the count.

```
/export.txt?scope=recent&n=50&fmt=markdown
```

## Look

Per-account: five themes, an accent override, comfortable or compact density,
three cover sizes and a motion toggle. Stored against the user, so everyone gets
their own.

## Layout

```
app/
  main.py       routes
  db.py         schema, settings, auth helpers
  slots.py      slot ledger
  queues.py     random slate draw and refill
  paste.py      markdown list parser and title matching
  metadata.py   IGDB lookups
  jobs.py       background library walk
  importer.py   CSV in
  exporter.py   CSV and plain text out
  templates/    server-rendered Jinja pages
  static/       one stylesheet
data/           SQLite database and exports
```
