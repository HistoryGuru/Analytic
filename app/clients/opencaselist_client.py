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
import difflib
import re
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = "https://api.opencaselist.com/v1"

_SUFFIX_RE = re.compile(r"\b(high school|senior high|jr\/sr|junior\/senior|hs|sr|jr)\b")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]")
_SPACE_RE = re.compile(r"\s+")


def _normalize_school(name: str) -> str:
    """Lowercase, strip common suffixes like 'HS'/'High School', and collapse
    punctuation/whitespace, so 'Lynbrook HS' and 'Lynbrook' compare equal."""
    s = name.lower().strip()
    s = _SUFFIX_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


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

    async def _get_schools(self, caselist_slug: str) -> List[Dict[str, Any]]:
        assert self._client is not None
        resp = await self._client.get(f"/caselists/{caselist_slug}/schools")
        if resp.status_code != 200:
            return []
        return resp.json()

    async def find_school(self, caselist_slug: str, school_query: str) -> Optional[str]:
        """Match a school name/code against the caselist's school list.
        Returns the exact `school` slug Opencaselist expects, or None."""
        schools = await self._get_schools(caselist_slug)
        q_raw = school_query.strip().lower()
        q_norm = _normalize_school(school_query)

        # 1. exact raw match
        for s in schools:
            if s.get("name", "").lower() == q_raw:
                return s["name"]

        # 2. normalized exact match (handles "Lynbrook HS" == "Lynbrook")
        for s in schools:
            if _normalize_school(s.get("name", "")) == q_norm:
                return s["name"]
            if s.get("displayName") and _normalize_school(s["displayName"]) == q_norm:
                return s["name"]

        # 3. bidirectional substring match, normalized (either side could be
        # the more specific one -- "Lynbrook" vs "Lynbrook Sr" for example)
        for s in schools:
            name_norm = _normalize_school(s.get("name", ""))
            display_norm = _normalize_school(s.get("displayName") or "")
            if not name_norm:
                continue
            if (
                (q_norm and name_norm and (q_norm in name_norm or name_norm in q_norm))
                or (q_norm and display_norm and (q_norm in display_norm or display_norm in q_norm))
            ):
                return s["name"]

        return None

    async def suggest_schools(self, caselist_slug: str, school_query: str, limit: int = 5) -> List[str]:
        """When find_school comes up empty, return the closest listed school
        names (by string similarity) so the caller can surface a useful
        'did you mean' message instead of a bare 404."""
        schools = await self._get_schools(caselist_slug)
        names = [s.get("name", "") for s in schools if s.get("name")]
        q_norm = _normalize_school(school_query)
        normalized_map = {_normalize_school(n): n for n in names}
        close = difflib.get_close_matches(q_norm, list(normalized_map.keys()), n=limit, cutoff=0.4)
        return [normalized_map[c] for c in close]

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

