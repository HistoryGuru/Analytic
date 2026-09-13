"""
Client for the real Opencaselist v1 API (api.opencaselist.com/v1).

Confirmed against the open-source `opencaselist-python` client
(https://github.com/illuminate-dev/opencaselist-python) -- we hand-roll our
own thin async version here (httpx instead of requests) rather than
depending on that package directly, both to keep this project's own
dependency footprint small and because that package pins Python >=3.13,
which is safer to avoid on a Render deploy right now.

Endpoints used:
  POST /login                                                  {username, password} -> session cookie/token
  GET  /caselists                                               -> list of caselists (e.g. hsld26)
  GET  /caselists/{slug}/schools                                -> schools in a caselist
  GET  /caselists/{slug}/schools/{school}/teams                 -> teams at a school
  GET  /caselists/{slug}/schools/{school}/teams/{team}/rounds    -> disclosed rounds for a team
       Round: {tournament, side, round, opponent, judge, report, tourn_id, external_id}

`report` is the free-text disclosure field where debaters post their case
name/tag/argument for that round -- this is our source of "what was read."
`tourn_id` / `external_id` are the join keys back into Tabroom.
"""
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = "https://api.opencaselist.com/v1"


class OpencaselistClient:
    def __init__(self, username: str, password: str, timeout: float = 20.0):
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "OpencaselistClient":
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)
        await self._login()
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    async def _login(self) -> None:
        assert self._client is not None
        resp = await self._client.post(
            "/login", json={"username": self.username, "password": self.password}
        )
        resp.raise_for_status()
        # The session's cookie jar now holds the auth cookie for subsequent calls.

    async def find_school(self, caselist_slug: str, school_query: str) -> Optional[str]:
        """Fuzzy-match a school name/code against the caselist's school list.
        Returns the exact `school` slug Opencaselist expects, or None."""
        assert self._client is not None
        resp = await self._client.get(f"/caselists/{caselist_slug}/schools")
        if resp.status_code != 200:
            return None
        schools = resp.json()
        q = school_query.strip().lower()
        # exact match first, then substring match
        for s in schools:
            if s.get("name", "").lower() == q:
                return s["name"]
        for s in schools:
            name = s.get("name", "").lower()
            display = (s.get("displayName") or "").lower()
            if q in name or q in display:
                return s["name"]
        return None

    async def find_team(
        self, caselist_slug: str, school_slug: str, debater_name: str
    ) -> Optional[Dict[str, Any]]:
        """Find the team entry at a school whose debater1/2 name matches."""
        assert self._client is not None
        resp = await self._client.get(f"/caselists/{caselist_slug}/schools/{school_slug}/teams")
        if resp.status_code != 200:
            return None
        teams = resp.json()
        q = debater_name.strip().lower()
        for t in teams:
            names = " ".join(
                filter(
                    None,
                    [
                        t.get("debater1_first"), t.get("debater1_last"),
                        t.get("debater2_first"), t.get("debater2_last"),
                    ],
                )
            ).lower()
            if q in names or q in (t.get("display_name") or t.get("name", "")).lower():
                return t
        return None

    async def get_team_rounds(
        self, caselist_slug: str, school_slug: str, team_slug: str
    ) -> List[Dict[str, Any]]:
        assert self._client is not None
        resp = await self._client.get(
            f"/caselists/{caselist_slug}/schools/{school_slug}/teams/{team_slug}/rounds"
        )
        if resp.status_code != 200:
            return []
        return resp.json()

    async def search_school_and_team(
        self, caselist_slug: str, school_query: str, debater_name: str
    ) -> Optional[Dict[str, Any]]:
        """Convenience: resolve school -> team -> rounds in one call."""
        school_slug = await self.find_school(caselist_slug, school_query)
        if not school_slug:
            return None
        team = await self.find_team(caselist_slug, school_slug, debater_name)
        if not team:
            return None
        rounds = await self.get_team_rounds(caselist_slug, school_slug, team["name"])
        return {"school": school_slug, "team": team, "rounds": rounds}
