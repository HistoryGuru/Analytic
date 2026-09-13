# LD Scout API

On-demand Lincoln-Douglas debate scouting backend. No debater database —
every request is a live lookup against upstream sources, joined and
summarized on the fly, then handed to a Groq-hosted LLM for scouting notes.

This is the **backbone only** (per the brief) — a JSON API with interactive
docs at `/docs`. No frontend yet.

## How it works

```
GET /api/debater?q=<name or school code>&school=<optional hint>
        │
        ├─ 1. Tournaments.Tech (tournaments.tech/query)
        │      Pre-aggregated Tabroom win/loss records, bids, OTR score.
        │      This is the "Debate Land"-style source — first choice
        │      because it saves us from crawling every tournament.
        │      Does NOT know what arguments were read.
        │
        ├─ 2. Opencaselist (api.opencaselist.com/v1)
        │      Disclosed cases/arguments per round, plus tourn_id +
        │      external_id — direct pointers back into Tabroom. Always
        │      queried, since it's the only source of "what was read."
        │
        ├─ 3. Join step: match rounds from (1) and (2) on tourn_id + side
        │      (+ opponent as tiebreaker) so each result gets tagged with
        │      the argument that produced it.
        │
        └─ 4. Fallback: if Tournaments.Tech has nothing, fall back fully
               to Opencaselist's tourn_id/external_id + a Tabroom HTML
               scrape (app/clients/tabroom_client.py) for win/loss on each
               disclosed round. This is the "manual approach" from the brief.
        │
        ▼
  Stats layer (app/services/stats.py): overall win %, most-read arguments,
  win % reading each argument, win % facing each argument (opponent's own
  disclosure, best-effort, capped to avoid request fan-out).
        │
        ▼
  Groq LLM (app/services/ai_notes.py): turns the structured stats into a
  scouting summary + suggested strategy.
```

Results are cached in-memory for `CACHE_TTL_SECONDS` (default 15 min) to
avoid re-scraping on repeat searches — there's still no persistent database,
this just smooths out duplicate requests. See `app/cache.py` for how to swap
in Redis later if you deploy multiple instances.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your Tabroom / Opencaselist / Groq credentials
python run.py          # http://localhost:8000/docs
```

## Deploying to Render

1. Push this repo to GitHub.
2. In Render, "New Web Service" → connect the repo → it'll pick up
   `render.yaml` automatically (Blueprint deploy), or set the build/start
   commands manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Add the env vars from `.env.example` in Render's dashboard (they're
   marked `sync: false` in `render.yaml` so Render will prompt for them
   rather than committing secrets).

## Known limitations / things to verify live

I built this against the real, documented shapes of the Opencaselist API and
the reference Tabroom client library, but a few pieces genuinely can't be
verified without live network access to tabroom.com and opencaselist.com
(this dev environment can't reach those domains):

- **`app/clients/tabroom_client.py` → `get_entry_round_result()`**: the URL
  path and HTML table selector for a single entry's round-by-round results
  are my best inference from Tabroom's own scraping patterns, not confirmed
  against a live page. Open a real tournament's results page, inspect the
  results table, and adjust the `table = soup.find(...)` line and cell
  ordering — it's isolated to ~10 lines with a `TODO(verify-live)` marker.
- **`DEBATELAND_CIRCUITS`**: Tournaments.Tech requires an exact circuit
  string per query and the full LD circuit enum isn't published in their
  docs. Defaults to `National,Local` — add more via the env var if a
  debater's tournaments aren't showing up.
- **`CASELIST_SLUG`**: guessed as `hsld26` for the 2025-26 season, following
  Opencaselist's `hsld24`/`hspolicy25`-style naming. Confirm via
  `GET /caselists` once you have API access.
- **Argument name clustering** (`app/services/stats.py`) uses character
  similarity (difflib), not semantic matching — it'll correctly merge
  "Util AC" / "UTIL AC" but won't know "Util AC" and "Utilitarianism AC" are
  the same case. Fine for most debaters' own consistent disclosure habits;
  worth swapping for embedding-based clustering later if it's not precise
  enough.
- **"Win % vs. arguments faced"** requires resolving the *opponent's* own
  Opencaselist disclosure for the same round, which is capped at 12
  opponent lookups per query to avoid an unbounded fan-out — increase
  `_MAX_OPPONENT_LOOKUPS` in `lookup.py` if needed.

## Project layout

```
app/
  main.py              FastAPI app + CORS
  config.py            Settings from env vars
  cache.py             In-memory TTL cache
  schemas.py           Response models (DebaterSummary, RoundResult, etc.)
  clients/
    debateland_client.py    Tournaments.Tech (aggregated records)
    opencaselist_client.py  Opencaselist (disclosed arguments)
    tabroom_client.py       Tabroom private API + HTML fallback
  services/
    lookup.py            Orchestration: try aggregator, join, fall back
    stats.py              Win %, most-read arguments, clustering
    ai_notes.py           Groq call → scouting notes
  routers/
    debater.py             GET /api/debater
```

## Next steps (not built yet, by design)

- Frontend (simple search box + results view) — backend was the priority.
- `/api/debater/search` for disambiguation when multiple debaters match a
  name (the client libraries already support returning multiple matches;
  just needs a router + schema).
- Redis-backed cache if you outgrow a single Render instance.
