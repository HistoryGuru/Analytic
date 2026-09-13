# LD Scout API

On-demand Lincoln-Douglas debate scouting backend. No debater database —
every request is a live lookup against upstream sources, joined and
summarized on the fly, then handed to a Groq-hosted LLM for scouting notes.

This is the **backbone only** (per the brief) — a JSON API with interactive
docs at `/docs`. No frontend yet.

## How it works

Note: an earlier version of this tried Tournaments.Tech first as a
pre-aggregated shortcut for the win/loss record. That's been removed —
Tournaments.Tech turns out to be a Public Forum results database, not LD.
Opencaselist + Tabroom is now the only path.

```
GET /api/debater?q=<name>&school=<school, effectively required>
        │
        ├─ 1. Opencaselist (api.opencaselist.com/v1)
        │      Organized by school -> team -> rounds. Each disclosed round
        │      carries the case/argument read AND Tabroom's own tourn_id +
        │      external_id -- our join key back into Tabroom. Opencaselist
        │      has no cross-school name search, so `school` is required in
        │      practice (see the warning the API returns if you omit it).
        │
        ├─ 2. Tabroom (www.tabroom.com -- production, NOT staging.tabroom.com;
        │      staging is Tabroom's own internal test server for the
        │      platform itself, unrelated to any individual tournament)
        │      For each disclosed round, scrape the public results page for
        │      that tourn_id/external_id to get the win/loss. Tabroom has no
        │      public API for this, so this is a real HTML scrape
        │      (app/clients/tabroom_client.py) -- see the caveat below about
        │      the selector needing live verification.
        │
        └─ 3. Best-effort, capped: resolve a handful of OPPONENTS' own
               Opencaselist disclosure for the same rounds, to compute
               "win % when facing argument X."
        │
        ▼
  Stats layer (app/services/stats.py): overall win %, most-read arguments,
  win % reading each argument, win % facing each argument.
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
- **`school` is effectively required**: Opencaselist only supports
  school → team lookups, not a global name search, so without a school hint
  the app can only guess by treating your query as a school name (which
  rarely works). The frontend now requires it in the form.
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
    opencaselist_client.py  Opencaselist (disclosed arguments, join anchor)
    tabroom_client.py       Tabroom private API + HTML scrape (win/loss)
  services/
    lookup.py             Orchestration: Opencaselist -> Tabroom join
    stats.py              Win %, most-read arguments, clustering
    ai_notes.py           Groq call → scouting notes
  routers/
    debater.py             GET /api/debater
```

## Frontend

`frontend/` is a plain HTML/CSS/JS page (no build step) — a search box, a
loading state, an error state, and a report view with the record, most-read
arguments, win rate vs. arguments faced, the AI scouting notes, and a
collapsible round-by-round log.

Before deploying, point it at your backend:

```js
// frontend/config.js
window.LD_SCOUT_API_BASE = "https://your-backend.onrender.com";
```

Run it locally with any static server, e.g. `python3 -m http.server 8123`
from inside `frontend/`, then open `http://localhost:8123`.

**Deploying to Render**: "New Static Site" → connect the repo → set the
publish directory to `frontend`. No build command needed. Make sure the
backend's `CORS_ORIGINS` env var includes the static site's URL (or leave
it as `*` while you're testing).

## Next steps (not built yet, by design)

- `/api/debater/search` for disambiguation when multiple debaters match a
  name (the client libraries already support returning multiple matches;
  just needs a router + schema, and the frontend would need a picker UI).
- Redis-backed cache if you outgrow a single Render instance.
- Loading-state copy currently cycles through fixed messages; wiring real
  progress (e.g. "found on Opencaselist, now checking Tabroom…") would need
  the backend to stream status, which is a bigger change.
- A brute-force "search every school in the caselist for this name" mode,
  for when the school isn't known — deliberately not built since it'd mean
  hundreds of requests per lookup, which cuts against the on-demand design.
