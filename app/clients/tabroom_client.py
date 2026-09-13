"""
Client for Tabroom.

Tabroom has no official public API. There IS a private, cookie-authenticated
API at api.tabroom.com/v1 that the Tabroom web client itself uses (confirmed
via the open-source `tabroom` PyPI package's source) -- login happens
against the *main* site, not the API host:

    POST https://www.tabroom.com/user/login/login_save.mhtml
         form data: {username, password}
         -> sets a `TabroomToken` cookie, which is then sent on all
            api.tabroom.com/v1/* requests.

That private API covers tab-room operations (dashboards, attendance,
judge check-in) but does NOT expose a "win/loss record for entry X" route.
For that, Tabroom's own developer tooling falls back to scraping public
result HTML pages with BeautifulSoup -- e.g. `get_bids()` and
`get_teams_attending()` in the reference client scrape
`/index/results/toc_bids.mhtml` and `/index/tourn/fields.mhtml` respectively.

We follow the same pattern here for the page that actually matters to us:
the per-entry round-by-round result page for a given tournament. Tabroom
identifies an entry's results with a `tourn_id` (which tournament) and an
entry/student id -- both of which Opencaselist's `Round.tourn_id` /
`Round.external_id` fields give us directly, which is the whole point of
using Opencaselist as our join anchor in the manual-fallback path.

*** IMPORTANT / HONEST CAVEAT ***
Tabroom's result-page HTML (URL pattern, table structure, column order) is
undocumented, unversioned, and does change over time -- the reference
`ExtraResource` methods above hardcode `row.contents[N]` indices for that
exact reason. Rather than guess at brittle selectors I can't test against
the live site from here (tabroom.com isn't reachable from this sandbox),
this client:
  1. implements login + the private API wrapper (which IS stable), and
  2. implements `get_entry_round_result()` with the request shape and a
     parsing strategy that's easy to correct in ~10 lines once you view a
     real results page's HTML in your browser and confirm the selectors.
     A `raise NotImplementedError` with a clear TODO marks exactly where.
"""
from typing import Any, Dict, Optional

import httpx
from bs4 import BeautifulSoup

SITE_BASE_URL = "https://www.tabroom.com"
API_BASE_URL = "https://api.tabroom.com/v1"


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

    async def get_entry_round_result(
        self, tourn_id: int, entry_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch round-by-round results (win/loss per round) for one entry at
        one tournament, by scraping Tabroom's public results HTML.

        Returns a dict like:
            {"rounds": [{"round": "Round 3", "opponent": "...",
                         "judge": "...", "result": "Win"}, ...]}
        or None if the page couldn't be parsed.
        """
        assert self._client is not None

        # This is the public "results by entry" page. Confirm/adjust this
        # path against a real tournament once deployed -- Tabroom's own
        # scraping utilities use very similar `.mhtml` result endpoints
        # (see module docstring), but the exact route for a single entry's
        # round history isn't published anywhere we could verify from here.
        url = f"{SITE_BASE_URL}/index/results/index.mhtml"
        params = {"tourn_id": tourn_id, "entry_id": entry_id}

        resp = await self._client.get(url, params=params)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # TODO(verify-live): Once you can view a real results page, replace
        # this with the real table id/class. A good approach: open a
        # tournament's public results page in your browser, find the table
        # showing this entry's round-by-round record, right click ->
        # Inspect, and grab its `id` (Tabroom's own `ExtraResource` examples
        # use patterns like `soup.find(id="fieldsort")`).
        table = soup.find("table", id="entryresults")
        if table is None or table.tbody is None:
            return None

        rounds = []
        for row in table.tbody.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) < 4:
                continue
            rounds.append(
                {
                    "round": cells[0],
                    "opponent": cells[1],
                    "judge": cells[2],
                    "result": cells[3],
                }
            )

        return {"rounds": rounds} if rounds else None
