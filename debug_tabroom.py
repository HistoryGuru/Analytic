"""
Standalone diagnostic tool -- NOT part of the FastAPI app.

Run this locally (where you have real network access to tabroom.com,
unlike the sandbox this project was built in) to find out what Tabroom's
actual results-page HTML looks like for one of your disclosed rounds, so
we can fix the guessed selector in app/clients/tabroom_client.py with real
data instead of another guess.

Usage:
    python debug_tabroom.py --school "Lynbrook HS" --name "Jason Rong"

What it does:
  1. Logs into Opencaselist and pulls that debater's disclosed rounds
     (same code path the real app uses) to get a real tourn_id + external_id
     pair to test against.
  2. Logs into Tabroom.
  3. Tries several plausible URL patterns for that tourn_id/external_id and
     reports, for each: HTTP status, response length, and whether the
     words "Win"/"Loss" appear anywhere in the page (a quick signal that
     it's even the right kind of page).
  4. Saves the full HTML of each attempt to ./tabroom_debug/ so you can
     open it in a browser or a text editor and find the real table.

Once you know which URL pattern actually works and what the results table
looks like, send me either the saved HTML (or just the relevant <table>
snippet + the URL that worked) and I'll fix get_entry_round_result() for
real.
"""
import argparse
import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

SITE_BASE_URL = "https://www.tabroom.com"
DEBUG_DIR = Path("tabroom_debug")

CANDIDATE_PATHS = [
    ("/index/results/index.mhtml", lambda tourn_id, entry_id: {"tourn_id": tourn_id, "entry_id": entry_id}),
    ("/index/results/index.mhtml", lambda tourn_id, entry_id: {"tourn_id": tourn_id, "student_id": entry_id}),
    ("/index/results/team_results.mhtml", lambda tourn_id, entry_id: {"tourn_id": tourn_id, "team_id": entry_id}),
    ("/index/results/results_by_round.mhtml", lambda tourn_id, entry_id: {"tourn_id": tourn_id}),
    ("/index/tourn/results.mhtml", lambda tourn_id, entry_id: {"tourn_id": tourn_id}),
    ("/index/tourn/student_index.mhtml", lambda tourn_id, entry_id: {"tourn_id": tourn_id, "student_id": entry_id}),
]


async def get_a_real_round():
    """Reuse the app's own Opencaselist client to get a real tourn_id/external_id."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from app.clients.opencaselist_client import OpencaselistClient  # noqa: E402

    parser = argparse.ArgumentParser()
    parser.add_argument("--school", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--caselist-slug", default=os.environ.get("CASELIST_SLUG", "hsld26"))
    args = parser.parse_args()

    async with OpencaselistClient(
        os.environ["OPENCASELIST_USERNAME"], os.environ["OPENCASELIST_PASSWORD"]
    ) as oc:
        school_slug = await oc.find_school(args.caselist_slug, args.school)
        if not school_slug:
            print(f"Couldn't find school '{args.school}' on Opencaselist. Fix that first.")
            return None
        team = await oc.find_team(args.caselist_slug, school_slug, args.name)
        if not team:
            print(f"Found school '{school_slug}' but no team matching '{args.name}'.")
            return None
        rounds = await oc.get_team_rounds(args.caselist_slug, school_slug, team["name"])
        candidates = [r for r in rounds if r.get("tourn_id") and r.get("external_id")]
        if not candidates:
            print("Found the team, but no disclosed round has both tourn_id and external_id set.")
            print(f"Raw rounds: {rounds}")
            return None
        print(f"Using round: {candidates[0]}")
        return candidates[0]


async def try_tabroom_urls(tourn_id: int, entry_id: int):
    username = os.environ["TABROOM_USERNAME"]
    password = os.environ["TABROOM_PASSWORD"]

    DEBUG_DIR.mkdir(exist_ok=True)

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        login_resp = await client.post(
            f"{SITE_BASE_URL}/user/login/login_save.mhtml",
            data={"username": username, "password": password},
        )
        print(f"Login response status: {login_resp.status_code}")
        print(f"Cookies received: {list(client.cookies.keys())}")
        if "TabroomToken" not in client.cookies:
            print(
                "WARNING: no 'TabroomToken' cookie came back -- login likely failed "
                "(wrong credentials, or the login endpoint/field names have changed)."
            )

        for i, (path, param_fn) in enumerate(CANDIDATE_PATHS):
            params = param_fn(tourn_id, entry_id)
            resp = await client.get(f"{SITE_BASE_URL}{path}", params=params)
            has_win = "Win" in resp.text or "win" in resp.text.lower()
            has_loss = "Loss" in resp.text or "loss" in resp.text.lower()
            out_file = DEBUG_DIR / f"attempt_{i}_{path.strip('/').replace('/', '_')}.html"
            out_file.write_text(resp.text, encoding="utf-8")
            print(
                f"[{i}] GET {path} params={params} -> status={resp.status_code} "
                f"len={len(resp.text)} contains_win={has_win} contains_loss={has_loss} "
                f"saved_to={out_file}"
            )


async def main():
    round_data = await get_a_real_round()
    if not round_data:
        return
    await try_tabroom_urls(round_data["tourn_id"], round_data["external_id"])
    print(f"\nDone. Open the files in ./{DEBUG_DIR}/ and look for the round-by-round results table.")
    print("Send me the URL that actually shows this round's win/loss, plus that table's HTML.")


if __name__ == "__main__":
    asyncio.run(main())
