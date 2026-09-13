"""
Orchestrates a single debater lookup end-to-end.

Opencaselist is the anchor: it's organized by school -> team -> rounds, and
each disclosed round carries Tabroom's own tourn_id + external_id. So the
flow is:

  1. Resolve the debater's school + team on Opencaselist (requires a school
     hint -- Opencaselist has no cross-school name search, only
     school -> teams, so without a school we can only guess by treating the
     query itself as a school name, which rarely works).
  2. For each disclosed round, scrape Tabroom (www.tabroom.com, NOT
     staging.tabroom.com -- staging is Tabroom's own internal test server
     for the platform itself, unrelated to any individual tournament) for
     the win/loss on that specific round, using the tourn_id/external_id
     Opencaselist gave us.
  3. Compute overall record + most-read arguments + win % per argument from
     the joined rows.
  4. Best-effort, capped: resolve a handful of opponents' own Opencaselist
     disclosure for the same rounds, to compute "win % facing argument X".

(We previously tried Tournaments.Tech first as a pre-aggregated shortcut --
removed, since it's a Public Forum results database and doesn't cover LD.)
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from app.clients.opencaselist_client import OpencaselistClient
from app.clients.tabroom_client import TabroomClient
from app.config import Settings
from app.schemas import DebaterSummary, RoundResult
from app.services import stats as stats_service

_MAX_OPPONENT_LOOKUPS = 12  # cap on "argument faced" resolution per query


def _normalize_side(side: Optional[str]) -> Optional[str]:
    if not side:
        return None
    s = side.strip().lower()
    if s in ("a", "aff", "pro"):
        return "Aff"
    if s in ("n", "neg", "con"):
        return "Neg"
    return side


async def _resolve_rounds(
    settings: Settings, school_hint: str, debater_name: str
) -> Tuple[Optional[str], List[RoundResult]]:
    """Opencaselist rounds (source of truth for which tournaments/rounds
    happened + what was read) joined with Tabroom's scraped win/loss for
    each of those rounds via tourn_id/external_id.

    Returns (school_slug_matched, rounds).
    """
    rounds: List[RoundResult] = []

    async with OpencaselistClient(
        settings.opencaselist_username, settings.opencaselist_password
    ) as oc:
        found = await oc.search_school_and_team(settings.caselist_slug, school_hint, debater_name)
        if not found:
            return None, rounds
        oc_rounds = found["rounds"]
        school_slug = found["school"]

    async with TabroomClient(settings.tabroom_username, settings.tabroom_password) as tb:
        for r in oc_rounds:
            tourn_id = r.get("tourn_id")
            entry_id = r.get("external_id")
            result_val = None
            if tourn_id and entry_id:
                scraped = await tb.get_entry_round_result(tourn_id, entry_id)
                if scraped:
                    # match this specific round by round name within the scrape
                    for sr in scraped["rounds"]:
                        if sr.get("round", "").strip().lower() == (r.get("round") or "").strip().lower():
                            result_val = sr.get("result")
                            break

            rounds.append(
                RoundResult(
                    tournament=r.get("tournament"),
                    round_name=r.get("round"),
                    side=_normalize_side(r.get("side")),
                    opponent=r.get("opponent"),
                    judge=r.get("judge"),
                    result=result_val,
                    argument=r.get("report"),
                    tourn_id=tourn_id,
                    source_note="opencaselist + tabroom",
                )
            )

    return school_slug, rounds


async def lookup_debater(
    settings: Settings, query: str, school_hint: Optional[str] = None
) -> DebaterSummary:
    warnings: List[str] = []
    data_sources: List[str] = []

    if not school_hint:
        warnings.append(
            "No school provided -- Opencaselist is organized by school, not by a global name "
            "search, so results will be much more reliable if you include the debater's school."
        )

    school_for_oc = school_hint or query

    try:
        school_matched, rounds = await _resolve_rounds(settings, school_for_oc, query)
    except Exception as exc:
        rounds = []
        school_matched = None
        warnings.append(f"Lookup failed while contacting Opencaselist or Tabroom: {exc}")

    if rounds:
        data_sources.extend(["opencaselist", "tabroom"])
    elif not warnings or school_hint:
        warnings.append(
            "No disclosure found on Opencaselist for that name/school -- this debater may not be "
            "disclosing, may compete on a different circuit, or the school name didn't match "
            "closely enough (try the exact name Opencaselist lists, e.g. 'Harvard-Westlake' not 'HW')."
        )

    overall_record, win_pct = stats_service.compute_overall_record(rounds)
    most_read, by_win_pct = stats_service.compute_argument_stats(rounds)

    if rounds and not any(r.result for r in rounds):
        warnings.append(
            "Found disclosed rounds on Opencaselist, but couldn't confirm win/loss from Tabroom "
            "for any of them -- the Tabroom results-page scraper may need its selector adjusted "
            "for this tournament's page layout (see tabroom_client.py)."
        )

    # --- Best effort, capped: argument-faced win rates ---
    faced_pairs: List[Tuple[str, Optional[str]]] = []
    rounds_with_opponents = [r for r in rounds if r.opponent and r.result][:_MAX_OPPONENT_LOOKUPS]
    if rounds_with_opponents:
        try:
            async with OpencaselistClient(
                settings.opencaselist_username, settings.opencaselist_password
            ) as oc:
                for r in rounds_with_opponents:
                    opp_school_guess = r.opponent or ""
                    opp_found = await oc.search_school_and_team(
                        settings.caselist_slug, opp_school_guess, opp_school_guess
                    )
                    if not opp_found:
                        continue
                    for oc_round in opp_found["rounds"]:
                        if oc_round.get("tourn_id") == r.tourn_id and _normalize_side(
                            oc_round.get("side")
                        ) != r.side:
                            faced_pairs.append((oc_round.get("report"), r.result))
                            break
        except Exception:
            warnings.append("Couldn't resolve opponents' disclosures for argument-faced stats this time.")

    win_pct_vs_faced = stats_service.compute_opponent_argument_stats(faced_pairs)
    if rounds and not win_pct_vs_faced:
        warnings.append(
            "Win % vs. arguments faced needs opponents' own disclosure for the same rounds, "
            "which wasn't resolvable here (often because the opponent isn't disclosing)."
        )

    return DebaterSummary(
        query=query,
        matched_name=query,
        school=school_matched or school_hint,
        overall_record=overall_record,
        win_pct=win_pct,
        most_read_arguments=most_read,
        win_pct_by_argument_read=by_win_pct,
        win_pct_vs_argument_faced=win_pct_vs_faced,
        rounds=rounds,
        data_sources=data_sources,
        warnings=warnings,
    )
