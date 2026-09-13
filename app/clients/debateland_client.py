"""
Client for Tournaments.Tech's public read-only REST API
(https://docs.debate.land), which pre-aggregates Tabroom results into
win/loss records, bids, and OTR score per entry. This is our first-choice
source for a debater's overall record, since it saves us from having to
crawl every tournament ourselves.

Docs: https://docs.debate.land/documentation, https://docs.debate.land/schemas

No auth required -- it's a public read API. We deliberately keep this
client dependency-free (just httpx) since it's the "happy path" and should
be fast and simple.

NOTE: `circuit` is a required query param on their /query endpoint and the
full enum of valid circuit strings per format isn't published in the docs
we could access. `DEBATELAND_CIRCUITS` in config.py lets you configure which
circuit(s) to try, in order, without a code change. If a tournament shows up
under a circuit not in that list, add it there.
"""
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = "https://tournaments.tech"
EVENT_NAME = "Lincoln Douglas"


class DebateLandClient:
    def __init__(self, season: str, circuits: List[str], timeout: float = 15.0):
        self.season = season
        self.circuits = circuits
        self.timeout = timeout

    async def query_debater(self, term: str) -> Optional[Dict[str, Any]]:
        """
        Search for a debater/entry by name or code across the configured
        circuits. Returns the first (best) matching team dict, or None if
        nothing was found in any circuit.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for circuit in self.circuits:
                params = {
                    "format": EVENT_NAME,
                    "circuit": circuit,
                    "year": self.season,
                    "term": term,
                }
                try:
                    resp = await client.get(f"{BASE_URL}/query", params=params)
                except httpx.HTTPError:
                    continue

                if resp.status_code != 200:
                    continue

                try:
                    results = resp.json()
                except ValueError:
                    continue

                if isinstance(results, list) and results:
                    # Take the best/first match. If there are multiple
                    # plausible matches, the caller can widen the search
                    # via /api/debater/search instead.
                    match = results[0]
                    match["_circuit_matched"] = circuit
                    return match

        return None

    async def search_debaters(self, term: str) -> List[Dict[str, Any]]:
        """Return ALL matches across configured circuits, for disambiguation."""
        all_matches: List[Dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for circuit in self.circuits:
                params = {
                    "format": EVENT_NAME,
                    "circuit": circuit,
                    "year": self.season,
                    "term": term,
                }
                try:
                    resp = await client.get(f"{BASE_URL}/query", params=params)
                except httpx.HTTPError:
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    results = resp.json()
                except ValueError:
                    continue
                if isinstance(results, list):
                    for r in results:
                        r["_circuit_matched"] = circuit
                    all_matches.extend(results)
        return all_matches
