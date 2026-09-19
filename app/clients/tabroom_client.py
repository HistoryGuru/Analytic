"""
Client for Tabroom.

Rewritten against REAL, inspected HTML (previous version guessed at
selectors it couldn't verify -- this one is confirmed via browser
DevTools against live pages). Two pages matter, and both are plain
server-rendered HTML with no JavaScript/XHR involved -- confirmed by
checking the Network tab while the page "expanded" a section and seeing
zero new requests fire:

1. GET /index/tourn/fields.mhtml?tourn_id={tourn_id}&event_id={event_id}
   The tournament's entry list FOR ONE EVENT -- both params are required;
   tourn_id alone is just a landing page listing that tournament's events
   as links (confirmed: a tournament can have more than one LD-flavored
   event, e.g. both "Lincoln Douglas Round Robin" and "Varsity LD" on the
   same tournament -- search_tournament()/find_entry_id() try each).
   Once event_id is included, it's a completely ordinary HTML table:
     <tr class="odd"> (or similar -- see caveat below)
       <td>{institution}</td><td class="centeralign">{location}</td>
       <td>{entry name}</td><td>{code}</td><td class="centeralign">{status}</td>
       <td class="centeralign">
         <a class="buttonwhite greentext fa fa-table fa-sm" target="_blank"
            href="index/results/team_results.mhtml?id1={id1}&id2=">
       </td>
     </tr>
   `id1` is the entry's persistent Tabroom ID -- the same one shows up on
   every tournament that entry competes in.

   *** CAVEAT: the `role="row"` attribute shown in early screenshots was
   captured via Chrome DevTools' live DOM inspector, which reflects the
   page AFTER JavaScript runs -- confirmed (the hard way) that this
   attribute does not necessarily exist in the raw HTML this scraper
   actually receives. _find_entry_row() matches on plain <tr> now, not
   that attribute. ***

   Also confirmed: Tabroom's server remembers the last-selected event_id
   PER SESSION -- after fetching one event_id successfully, a subsequent
   bare tourn_id-only request can return that same event's table too.
   Don't rely on this; always pass event_id explicitly.

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
import json
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


def _extract_round_data_object(script_text: str) -> Optional[Dict[str, Any]]:
    """
    team_results.mhtml's round-by-round data is embedded as a JSON object
    literal directly inside a <script> tag (round_id -> round data),
    rendered client-side by React rather than existing as real HTML table
    markup. This scans for '{' characters and, at each one, extracts a
    balanced substring (tracking string literals so braces inside quoted
    values don't throw off the count) and tries to parse it as JSON.

    Confirmed real values use double-quoted JSON-compatible syntax (not
    single-quoted JS object shorthand), so plain json.loads works once the
    correct boundaries are found -- no JS-specific parsing needed.

    Returns the first successfully-parsed dict whose values all look like
    round records (each is itself a dict containing an "opponent" key),
    which distinguishes the real data object from small incidental dicts
    elsewhere in the same script (JSX prop objects, etc.).
    """
    n = len(script_text)
    i = 0
    while i < n:
        if script_text[i] != "{":
            i += 1
            continue

        depth = 0
        in_string = False
        escape = False
        string_char = ""
        j = i
        closed_at = None
        while j < n:
            c = script_text[j]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == string_char:
                    in_string = False
            else:
                if c == '"' or c == "'":
                    in_string = True
                    string_char = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        closed_at = j
                        break
            j += 1

        if closed_at is not None:
            candidate = script_text[i : closed_at + 1]
            try:
                obj = json.loads(candidate)
            except (ValueError, json.JSONDecodeError):
                obj = None
            if (
                isinstance(obj, dict)
                and obj
                and all(isinstance(v, dict) and "opponent" in v for v in obj.values())
            ):
                return obj

        i += 1

    return None


def _normalize_embedded_round(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map the embedded JSON round record's field names onto the plain
    dict shape the rest of this app expects."""
    decision_raw = str(data.get("decision_str") or "").strip().lower()
    result = _RESULT_MAP.get(decision_raw[:1]) if decision_raw else None

    round_label = data.get("round_label")
    round_name = data.get("round_name")
    round_display = str(round_label if round_label not in (None, "") else round_name or "")

    return {
        "tournament": data.get("tourn"),
        "round": round_display,
        "side": data.get("side"),
        "opponent": data.get("opponent"),
        "judge": data.get("judge_raw") or data.get("judge"),
        "result": result,
        "event_name": data.get("event_name"),
    }


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
        """
        Confirmed against the real login form's HTML (fetched fresh from
        the homepage, where the login widget lives): it includes hidden
        `salt` and `sha` fields alongside `username`/`password` that must
        be echoed back unchanged -- almost certainly a CSRF/replay guard.
        Omitting them (the previous version of this method did) causes
        the login to silently fail with no error, just a normal-looking
        redirect.

        Also: staging's session cookie is named `TabroomStaging`, NOT
        `TabroomToken` (that name is production's). Checking only for
        `TabroomToken` made every staging login look failed even when it
        may have actually succeeded.
        """
        assert self._client is not None

        login_page = await self._client.get(f"{SITE_BASE_URL}/")
        soup = BeautifulSoup(login_page.text, "html.parser")

        salt = sha = None
        for form in soup.find_all("form"):
            if form.get("action") == "/user/login/login_save.mhtml":
                salt_input = form.find("input", {"name": "salt"})
                sha_input = form.find("input", {"name": "sha"})
                salt = salt_input.get("value") if salt_input else None
                sha = sha_input.get("value") if sha_input else None
                break

        data = {"username": self.username, "password": self.password}
        if salt and sha:
            data["salt"] = salt
            data["sha"] = sha

        resp = await self._client.post(f"{SITE_BASE_URL}/user/login/login_save.mhtml", data=data)
        resp.raise_for_status()
        self._authenticated = any(
            name in self._client.cookies for name in ("TabroomToken", "TabroomStaging")
        )

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

        Confirmed against a real tournament: `fields.mhtml?tourn_id=X`
        ALONE is just a landing page listing that tournament's events as
        links (e.g. "Varsity LD", "Lincoln Douglas Round Robin") -- it does
        NOT show an entries table until you also pass that event's
        `event_id`. A tournament can have more than one LD-flavored event
        (as seen: both "Lincoln Douglas Round Robin" AND "Varsity LD" on
        the same tournament), so rather than guess which one a given
        debater is in, this discovers every LD-relevant event_id from the
        landing page and tries each in turn.
        """
        assert self._client is not None

        landing_resp = await self._client.get(
            f"{SITE_BASE_URL}/index/tourn/fields.mhtml", params={"tourn_id": tourn_id}
        )
        if landing_resp.status_code != 200:
            return None

        landing_soup = BeautifulSoup(landing_resp.text, "html.parser")
        event_ids: List[int] = []
        for link in landing_soup.find_all("a", href=True):
            if "event_id=" not in link["href"]:
                continue
            link_text = link.get_text(strip=True).lower()
            if "lincoln douglas" in link_text or re.search(r"\bld\b", link_text):
                qs = parse_qs(urlparse(link["href"]).query)
                event_id_str = qs.get("event_id", [None])[0]
                if event_id_str:
                    event_ids.append(int(event_id_str))

        if not event_ids:
            # No event links found at all -- maybe this page format doesn't
            # require event_id for this tournament, so fall back to trying
            # the landing page's own HTML directly (covers the case where
            # fields.mhtml did return a full table without needing one).
            found = self._find_entry_row(landing_resp.text, school, debater_name)
            return found

        for event_id in event_ids:
            resp = await self._client.get(
                f"{SITE_BASE_URL}/index/tourn/fields.mhtml",
                params={"tourn_id": tourn_id, "event_id": event_id},
            )
            if resp.status_code != 200:
                continue
            found = self._find_entry_row(resp.text, school, debater_name)
            if found:
                return found

        return None

    @staticmethod
    def _find_entry_row(html: str, school: str, debater_name: str) -> Optional[int]:
        """Parse an entries table (real fields.mhtml?tourn_id=X&event_id=Y
        response) for the row matching school + debater, returning their
        persistent entry id (id1) off the "view record" link.

        Iterates every <tr> with no attribute filter -- an earlier version
        selected only `tr[role="row"]`, a selector copied from Chrome
        DevTools' live DOM inspector, which reflects the page AFTER
        JavaScript runs. That attribute may only get added client-side
        (common with table libraries) and might not exist in the raw HTML
        this scraper actually receives, which would make the row genuinely
        present in the response but invisible to that selector.
        """
        soup = BeautifulSoup(html, "html.parser")
        school_norm = school.strip().lower()
        name_norm = debater_name.strip().lower()

        for row in soup.find_all("tr"):
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

        CONFIRMED (the hard way -- an earlier version of this method tried
        to scrape rendered HTML tables, which don't really exist server-
        side): this page's round-by-round data is actually built entirely
        client-side by React from an embedded JSON object sitting inside a
        <script> tag, e.g.:

            "9067179": {"decision_str": "W", "opponent": "Harvard-Westlake SL",
                        "judge_raw": "Martinez, David ", "round_name": 1,
                        "side": "Aff", "tourn": "Loyola Invitational",
                        "tourn_id": 40342, "event_name": "Varsity LD", ...}

        keyed by round_id, mapping round_id -> round data. This is
        genuinely more reliable than scraping a rendered table would have
        been -- clean field names, no HTML entity/whitespace cleanup
        needed. _extract_round_data() finds and parses that object
        directly rather than looking for table markup.
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

        for script in soup.find_all("script"):
            content = script.string or ""
            if "opponent" not in content or "decision_str" not in content:
                continue
            round_map = _extract_round_data_object(content)
            if round_map:
                for round_id, data in round_map.items():
                    rounds.append(_normalize_embedded_round(data))
                break  # found the real data object, no need to check other scripts

        return rounds
