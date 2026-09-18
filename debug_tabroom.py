"""
Standalone diagnostic -- NOT part of the FastAPI app.

Run locally (real network access to tabroom.com required):
    python3 debug_tabroom.py --school "Lynbrook HS" --name "Jason Rong"

Deliberately calls the SAME OpencaselistClient / TabroomClient classes the
real app uses, step by step, rather than a separate reimplementation --
a prior version of this diagnostic tested login with its own hand-rolled
request and drifted out of sync with a real bug fix in the app itself,
which wasted a round of debugging. Using the real classes means this
script can never again disagree with what the app actually does.

Deliberately never touches www.tabroom.com -- confirmed via a prior run
that production drops scripted requests outright (RemoteProtocolError,
mid-connection disconnect -- a bot-defense signature, not a normal error).
Only staging.tabroom.com is used, per Tabroom's own guidance for
automated access.
"""
import argparse
import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from app.clients.opencaselist_client import OpencaselistClient  # noqa: E402
from app.clients.tabroom_client import TabroomClient, clean_tournament_name  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--caselist-slug", default=os.environ.get("CASELIST_SLUG", "hsld26"))
    args = parser.parse_args()

    print("=== Step 1: Opencaselist ===")
    async with OpencaselistClient(
        os.environ["OPENCASELIST_USERNAME"], os.environ["OPENCASELIST_PASSWORD"]
    ) as oc:
        school_slug = await oc.find_school(args.caselist_slug, args.school)
        print(f"find_school({args.school!r}) -> {school_slug!r}")
        if not school_slug:
            suggestions = await oc.suggest_schools(args.caselist_slug, args.school)
            print(f"Closest listed schools: {suggestions}")
            return

        team = await oc.find_team(args.caselist_slug, school_slug, args.name)
        print(f"find_team({school_slug!r}, {args.name!r}) -> {team!r}")
        if not team:
            return

        oc_rounds = await oc.get_team_rounds(args.caselist_slug, school_slug, team["name"])
        print(f"get_team_rounds(...) -> {len(oc_rounds)} round(s)")

    distinct_tournaments = []
    for r in oc_rounds:
        t = r.get("tournament")
        if t and t not in distinct_tournaments:
            distinct_tournaments.append(t)
    print(f"Distinct tournament names: {distinct_tournaments}")

    print("\n=== Step 2: Tabroom login ===")
    async with TabroomClient(os.environ["TABROOM_USERNAME"], os.environ["TABROOM_PASSWORD"]) as tb:
        print(f"tb.authenticated -> {tb.authenticated}")
        if not tb.authenticated:
            print("Login did not return a recognized auth cookie -- everything below will "
                  "likely fail, but continuing anyway to see how far it gets.")

        print("\n=== Step 3: tournament + entry resolution ===")
        entry_id = None
        for tourn_name in distinct_tournaments:
            clean = clean_tournament_name(tourn_name)
            print(f"\nsearch_tournament({tourn_name!r}) [cleaned: {clean!r}]")
            tourn_id = await tb.search_tournament(tourn_name)
            print(f"  -> tourn_id = {tourn_id}")
            if not tourn_id:
                continue

            found_id = await tb.find_entry_id(tourn_id, school_slug, args.name)
            print(f"find_entry_id(tourn_id={tourn_id}, school={school_slug!r}, name={args.name!r})")
            print(f"  -> entry_id = {found_id}")
            if found_id:
                entry_id = found_id
                break

        if not entry_id:
            print("\nNo entry_id resolved -- stopping here. The per-step output above shows "
                  "exactly which call returned nothing.")
            return

        print(f"\n=== Step 4: season rounds for entry_id={entry_id} ===")
        season_rounds = await tb.get_season_rounds(entry_id)
        print(f"get_season_rounds({entry_id}) -> {len(season_rounds)} round(s)")
        for r in season_rounds:
            print(f"  {r}")


if __name__ == "__main__":
    asyncio.run(main())
