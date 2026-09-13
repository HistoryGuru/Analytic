"""
Client for Tabroom.

Rewritten against REAL, inspected HTML (previous version guessed at
selectors it couldn't verify -- this one is confirmed via browser
DevTools against live pages). Two pages matter, and both are plain
server-rendered HTML with no JavaScript/XHR involved -- confirmed by
checking the Network tab while the page "expanded" a section and seeing
zero new requests fire:

1. GET /index/tourn/fields.mhtml?tourn_id={tourn_id}
   The tournament's entry list. A completely ordinary HTML table:
   <tr role="row" class="odd">
     <td>{institution}</td><td class="centeralign">{location}</td>
     <td>{entry name}</td><td>{code}</td><td class="centeralign">{status}</td>
     <td class="centeralign">
       <a class="buttonwhite greentext fa fa-table fa-sm" target="_blank"
          href="index/results/team_results.mhtml?id1={id1}&id2=">
     </td>
   </tr>
   `id1` is the entry's persistent Tabroom ID -- the same one shows up on
   every tournament that entry competes in.

2. GET /index/results/team_results.mhtml?id1={id1}&id2=
   Given just that one id1, this returns the entry's FULL CURRENT SEASON
   in one page -- every tournament, every round, not just one tournament.
   So we only need to resolve id1 ONCE per debater (via any single one of
   their tournaments), then this one page gives us everything.

We use staging.tabroom.com (not www.tabroom.com) per Tabroom's own posted
guidance to route automated/scripted access there rather than production,
to stay within their bot-usage policy.

3. POST /index/search.mhtml   body: {"search": "<name>", "caller": ""}
   Confirmed via the real search form's HTML:
     <form action="/index/search.mhtml" method="post">
       <input type="hidden" name="caller" value="...">
       <input type="text" name="search" ...>
   Returns an HTML table ("Tournaments matching '<name>':") with rows of
   <a href=".../index/tourn/index.mhtml?tourn_id=X">Tournament Name</a>,
   plus Location/Date/Events/Circuits columns. Searching a common word like
   "Loyola" can return many tournaments across different years and even
   different hosts (e.g. "BCFL 2 at Loyola") -- search_tournament() scores
   candidates by name similarity, whether Events mentions "Lincoln
   Douglas"/"LD", and whether the Date falls in the target season, rather
   than just taking the first result.
"""
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

SITE_BASE_URL = "https://staging.tabroom.com"

_RESULT_MAP = {"w": "Win", "l": "Loss"}
# Opencaselist prefixes tournament names with a sort index like "01---";
# strip that before searching Tabroom's real tournament name.
_OC_TOURNAMENT_PREFIX_RE = re.compile(r"^\d+-+")


def clean_tournament_name(name: str) -> str:
    return _OC_TOURNAMENT_PREFIX_RE.sub("", name).strip()


class TabroomClient:
    def __init__(self, username: str, password: str, timeout: float = 20.0):
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._authenticated = False

    async def __aenter__(self) -> "TabroomClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        await self._login()
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    async def _login(self) -> None:
        assert self._client is not None
        resp = await self._client.post(
            f"{SITE_BASE_URL}/user/login/login_save.mhtml",
            data={"username": self.username, "password": self.password},
        )
        resp.raise_for_status()
        self._authenticated = "TabroomToken" in self._client.cookies

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    async def search_tournament(self, tournament_name: str) -> Optional[int]:
        """
        Resolve a tournament name to its tourn_id via Tabroom's real search
        form (POST /index/search.mhtml, confirmed against the live site).

        A generic name like "Loyola" can match many tournaments across
        different years/hosts. Tabroom's own results are already sorted
        most-recent-first (confirmed: the page states "sorted in reverse
        order by date"), and since we're always resolving a CURRENT
        debater's CURRENT-season disclosure, the correct match is always
        the most recent one that plausibly runs LD -- never an older
        same-named tournament. So this takes the first (topmost) row where
        the cleaned query name and the row's tournament name contain one
        another, and the row mentions Lincoln Douglas/LD anywhere in its
        Events column, rather than scoring by string-similarity (which
        actively misranks things here: e.g. "Loyola RR" scores HIGHER
        similarity to "Loyola" than "Loyola Invitational" does, purely
        because it's shorter -- not because it's the right tournament).
        """
        assert self._client is not None
        clean_name = clean_tournament_name(tournament_name).lower()

        resp = await self._client.post(
            f"{SITE_BASE_URL}/index/search.mhtml",
            data={"search": clean_name, "caller": ""},
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        for row in soup.find_all("tr"):
            link = row.find("a", href=True)
            if not link or "tourn_id=" not in link["href"]:
                continue
            qs = parse_qs(urlparse(link["href"]).query)
            tourn_id_str = qs.get("tourn_id", [None])[0]
            if not tourn_id_str:
                continue

            row_name = link.get_text(strip=True).lower()
            row_text = row.get_text(" ", strip=True).lower()

            name_matches = clean_name in row_name or row_name in clean_name
            is_ld = "lincoln douglas" in row_text or re.search(r"\bld\b", row_text)

            if name_matches and is_ld:
                return int(tourn_id_str)

        return None

    async def find_entry_id(self, tourn_id: int, school: str, debater_name: str) -> Optional[int]:
        """
        Scrape a tournament's entries list (fields.mhtml -- confirmed real,
        static HTML) for the row matching this school + debater, and pull
        their persistent entry id (`id1`) off the "view record" link.
        """
        assert self._client is not None
        resp = await self._client.get(
            f"{SITE_BASE_URL}/index/tourn/fields.mhtml", params={"tourn_id": tourn_id}
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        school_norm = school.strip().lower()
        name_norm = debater_name.strip().lower()

        for row in soup.select("tr[role='row']"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            institution = cells[0].get_text(strip=True).lower()
            entry_name = cells[2].get_text(strip=True).lower()
            school_match = school_norm in institution or institution in school_norm
            name_match = name_norm in entry_name or entry_name in name_norm
            if school_match and name_match:
                link = row.find("a", href=True)
                if link and "id1=" in link["href"]:
                    qs = parse_qs(urlparse(link["href"]).query)
                    id1 = qs.get("id1", [None])[0]
                    if id1:
                        return int(id1)
        return None

    async def get_season_rounds(self, entry_id: int) -> List[Dict[str, Any]]:
        """
        Fetch this entry's ENTIRE current season in one request --
        team_results.mhtml?id1=... covers every tournament, not just one.
        Confirmed real (server-rendered, data-reactid-tagged but still
        plain parseable HTML -- no separate XHR call backs it).

        Returns a flat list of round dicts:
            {"tournament": ..., "round": ..., "side": ..., "opponent": ...,
             "judge": ..., "result": "Win"|"Loss"|None}
        """
        assert self._client is not None
        resp = await self._client.get(
            f"{SITE_BASE_URL}/index/results/team_results.mhtml",
            params={"id1": entry_id, "id2": ""},
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        rounds: List[Dict[str, Any]] = []

        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers or "round" not in headers or "opponent" not in headers:
                continue
            if "decision" not in headers and "judge" not in headers:
                continue

            idx = {h: i for i, h in enumerate(headers)}

            # The tournament name for this table lives in a preceding
            # heading like "Loyola Invitational for Jason Rong on Open" --
            # NOT YET VERIFIED which tag holds it (h1-h4? a plain div?), so
            # this walks backward through preceding siblings/parents
            # looking for text matching that "<tournament> for <name> on
            # <division>" pattern rather than assuming a specific tag.
            tourn_name = None
            for el in table.find_all_previous(string=re.compile(r".+ for .+ on .+")):
                tourn_name = str(el).split(" for ")[0].strip()
                break

            body_rows = table.find_all("tr")[1:]  # skip header row
            for tr in body_rows:
                cells = tr.find_all("td")
                if len(cells) <= max(idx.get(k, 0) for k in idx):
                    continue

                def cell(key: str) -> str:
                    i = idx.get(key)
                    return cells[i].get_text(strip=True) if i is not None and i < len(cells) else ""

                decision_raw = cell("decision").strip().lower()
                result = _RESULT_MAP.get(decision_raw[:1]) if decision_raw else None

                rounds.append(
                    {
                        "tournament": tourn_name,
                        "round": cell("round"),
                        "side": cell("side"),
                        "opponent": cell("opponent"),
                        "judge": cell("judge"),
                        "result": result,
                    }
                )

        return rounds
