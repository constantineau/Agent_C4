# Gemini UI griller

Google Gemini reviews the C4 Lab and the on-deck race console like a hostile senior UI/UX
reviewer, for a racing sailor on an iPad: glare, wet fingers, glance-reading, stale data, wrong
units. Built like Tramp's `tools/playtest-gemini/` (same SDK, model tiers, cost cap, dry run).

It takes screenshots of every Lab tab and of the console at three iPad sizes (1024x1366,
1366x1024, 820x1180) in several states, then sends each screen's screenshots plus the
front-end source behind it to Gemini in one call. Gemini answers with structured JSON findings:
title, screen, size, state, severity (blocker/major/minor), evidence on the screenshot,
suggested fix, `security`, and file/line when it can tell.

What it captures:

- **Lab** (`lab-*`): the standing dev stack on :8103, signed in with a token from
  `POST /api/auth`. States: `loaded`; `empty` (the browser answers `/api/races` with an empty
  library, nothing is written); `filtered` (Races: the second race; Polar: split by race and one
  race picked). Every non-GET request from the page is blocked, so a capture can't write to the
  Lab. Plus the signed-out gate (`lab-signin`).
- **Console** (`console`): the shipped iPad console replaying the Jul 18 Race Rewind timeline
  (`tools/replay/server.py`, started on a free port and stopped at the end). Moments: early race,
  22:08:31Z heading WATCH, 22:59:01Z GPS kick, 23:56:10Z compass out, 00:25Z "-90° off GPS
  course", and the same moment with the DATA tile's detail open.

States and source slices live in `src/screens.mjs`. Sources are cut by section-header text,
not line numbers; `npm test` fails if a header is renamed.

## Run it

```sh
tools/grill-ui-gemini/run.sh --dry-run                          # canned fake reviewer, no key, no cost
tools/grill-ui-gemini/run.sh --model cheap --only lab-polar,console --max-usd 0.5
tools/grill-ui-gemini/run.sh                                     # everything, strong model, $1 cap
tools/grill-ui-gemini/run.sh --shots /mnt/data/c4/ui-grill/<run> # re-grill old screenshots
tools/grill-ui-gemini/run.sh --help
```

Each run goes to `/mnt/data/c4/ui-grill/<time>/`: `shots/<screen>__<state>__<size>.png`,
`manifest.json` (every shot: URL, replay frame and its time, blocked writes, page errors),
`findings.json` (numbered G1, G2… by severity), `report.md` (public findings only; security
ones by id), `summary.json` (cost, tokens, counts, failures), `log.jsonl` (raw replies).

The findings are unverified model output. The `/grill-ui` skill verifies each one before
anything is filed. Security findings never go to GitHub (the repo is public).

## Model and cost

The key is `C4_GEMINI_API_KEY`, exported or in the repo's `.env` (the main checkout's, from a
worktree). It is never printed or written to a run. The Lab password comes from
`C4_LAB_PASSWORD`, `LAB_PASSWORD` in a `.env`, the running `sr33-dev-lab-1`, or the compose
default, in that order.

| `--model` | Model | $ per 1M in / out |
|---|---|---|
| `cheap` | gemini-3.5-flash-lite | 0.30 / 2.50 |
| `mid` | gemini-3.8-flash | 0.75 / 3.75 |
| `strong` (default) | gemini-4-argon | 2.00 / 10.00 |

Prices are Tramp's table (`src/models.mjs`, read 2026-10-16). Argon doubles on 2026-12-01. A
screen costs about 20-45k input tokens (source plus 3-18 screenshots at `--resolution high`).
The run prints tokens and cost and stops at `--max-usd` (default $1.00): each call is counted
by the API first and its output capped to what is left.

## Tests

`npm test` in this directory (findings parsing, the security split, cost cap, .env reading,
source markers). Not in `make ci`: it needs this directory's own `npm ci`.
