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
            else:
                # Dump the raw page so we can see what staging's fields.mhtml
                # actually looks like, rather than guessing a third time --
                # every prior screenshot of this page was from production,
                # not staging, and the two may not match structurally.
                from app.clients.tabroom_client import SITE_BASE_URL
                assert tb._client is not None
                raw_resp = await tb._client.get(
                    f"{SITE_BASE_URL}/index/tourn/fields.mhtml", params={"tourn_id": tourn_id}
                )
                out_path = f"tabroom_debug_fields_{tourn_id}.html"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(raw_resp.text)
                print(f"  Saved raw fields.mhtml response ({len(raw_resp.text)} chars) to {out_path}")
                print(f"  Status: {raw_resp.status_code}")
                print(f"  Contains {args.name!r}: {args.name in raw_resp.text}")
                print(f"  Contains {school_slug!r}: {school_slug in raw_resp.text}")
                print(f"  Number of <table> tags: {raw_resp.text.lower().count('<table')}")
                print(f"  Number of <tr tags: {raw_resp.text.lower().count('<tr')}")

                # fields.mhtml?tourn_id=X alone can be a landing page that
                # just lists available events/divisions as links, requiring
                # you to pick one before it shows an actual entries table.
                # Print every link on the page so we can see the real
                # parameter name/format for selecting an event.
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(raw_resp.text, "html.parser")
                event_links = [a for a in soup.find_all("a", href=True) if "fields.mhtml" in a["href"]]
                print(f"  Found {len(event_links)} link(s) containing 'fields.mhtml':")
                for a in event_links:
                    print(f"    {a.get_text(strip=True)!r} -> {a['href']}")

        if not entry_id:
            print("\nNo entry_id resolved -- stopping here. The per-step output above shows "
                  "exactly which call returned nothing.")
            return

        print(f"\n=== Step 4: season rounds for entry_id={entry_id} ===")
        season_rounds = await tb.get_season_rounds(entry_id)
        print(f"get_season_rounds({entry_id}) -> {len(season_rounds)} round(s)")
        for r in season_rounds:
            print(f"  {r}")

        # Panel (multi-judge) elimination rounds were one thing verified
        # already (fixed: now uses ballots_won/lost, not decision_str
        # character position). This section now checks a DIFFERENT thing:
        # whether merging every <script>'s data recovers tournaments that
        # a single-best-object approach was silently dropping.
        from app.clients.tabroom_client import SITE_BASE_URL, _extract_round_data_objects
        from bs4 import BeautifulSoup as _BS
        assert tb._client is not None
        raw_resp = await tb._client.get(
            f"{SITE_BASE_URL}/index/results/team_results.mhtml", params={"id1": entry_id, "id2": ""}
        )
        soup = _BS(raw_resp.text, "html.parser")
        merged: dict = {}
        for script in soup.find_all("script"):
            content = script.string or ""
            merged.update(_extract_round_data_objects(content))

        print(f"\n  Merged across all scripts: {len(merged)} total round entries found")
        tourns_seen = {}
        for round_id, data in merged.items():
            if isinstance(data, dict) and "opponent" in data:
                t = data.get("tourn")
                tourns_seen[t] = tourns_seen.get(t, 0) + 1
        print(f"  Tournaments represented in merged data: {tourns_seen}")

        print("\n  Raw (un-normalized) data for elimination rounds:")
        for round_id, data in merged.items():
            if isinstance(data, dict) and str(data.get("round_label") or data.get("round_name")) not in (
                "1", "2", "3", "4", "5", "6", "None",
            ):
                print(f"    round_id={round_id}: {data}")

        if not season_rounds:
            # Same principle as Step 3: don't guess a third time on this
            # page, look at the raw response directly.
            from app.clients.tabroom_client import SITE_BASE_URL
            assert tb._client is not None
            raw_resp = await tb._client.get(
                f"{SITE_BASE_URL}/index/results/team_results.mhtml",
                params={"id1": entry_id, "id2": ""},
            )
            out_path = f"tabroom_debug_team_results_{entry_id}.html"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(raw_resp.text)
            print(f"\n  Saved raw team_results.mhtml response ({len(raw_resp.text)} chars) to {out_path}")
            print(f"  Status: {raw_resp.status_code}")
            print(f"  Number of <table> tags: {raw_resp.text.lower().count('<table')}")
            print(f"  Number of <th tags: {raw_resp.text.lower().count('<th')}")
            print(f"  Number of <td tags: {raw_resp.text.lower().count('<td')}")
            print(f"  Number of <tr tags: {raw_resp.text.lower().count('<tr')}")
            print(f"  Contains 'Round': {'Round' in raw_resp.text}")
            print(f"  Contains 'Opponent': {'Opponent' in raw_resp.text}")
            print(f"  Contains 'Decision': {'Decision' in raw_resp.text}")
            print(f"  Contains 'Loyola': {'Loyola' in raw_resp.text}")

            # 11 <table> tags but almost no <tr>/<td>/<th> strongly suggests
            # those aren't real HTML tables at all -- likely React
            # components styled to look like tables. The actual data is
            # probably embedded as JSON inside a <script> tag instead
            # (consistent with the data-reactid attributes seen earlier,
            # a marker of old-style React server-rendering that typically
            # ships its initial data as inline JSON for hydration).
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw_resp.text, "html.parser")
            scripts = soup.find_all("script")
            print(f"\n  Found {len(scripts)} <script> tag(s). Previewing each:")
            for i, script in enumerate(scripts):
                content = script.string or ""
                preview = content[:150].replace("\n", " ")
                print(f"    Script #{i}: {len(content)} chars -- {preview!r}")

            # Script #6 (component definitions) contains "Opponent" only as
            # a column-header LABEL, not per-round data -- search all
            # scripts for a real opponent name we already know exists for
            # this debater (from Opencaselist's own disclosure), which
            # should pinpoint whichever script actually holds the data
            # object, wherever it is.
            known_opponents = ["Harvard-Westlake", "Jericho", "Peninsula", "BASIS", "Immaculate Heart", "Dougherty Valley"]
            print(f"\n  Searching all scripts for known opponent names {known_opponents}...")
            for i, script in enumerate(scripts):
                content = script.string or ""
                hits = [name for name in known_opponents if name in content]
                if hits:
                    print(f"\n  --- Script #{i} contains real opponent name(s): {hits} ---")
                    idx = content.find(hits[0])
                    print(f"  Context around {hits[0]!r}:")
                    print("  " + content[max(0, idx - 300):idx + 300].replace("\n", " "))
                    print("  --- end context ---")


if __name__ == "__main__":
    asyncio.run(main())
