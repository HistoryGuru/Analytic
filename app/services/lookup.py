"""
Orchestrates a single debater lookup end-to-end:

  1. Try Tournaments.Tech (the pre-aggregated, Debate-Land-style source) for
     the overall win/loss record and round-by-round results.
  2. Always query Opencaselist too (independently) for disclosed
     arguments/cases per round -- Tournaments.Tech doesn't have this data at
     all, so this step isn't really a "fallback", it's a second source we
     join against.
  3. Join the two by (tourn_id, side, round-name) where possible so each
     round result gets tagged with what was actually read.
  4. If Tournaments.Tech had nothing, fall back fully to the manual path:
     Opencaselist rounds (which include Tabroom's own tourn_id/external_id)
     + TabroomClient HTML scraping for the win/loss on each of those rounds.
  5. Best-effort: for a handful of rounds, also resolve the OPPONENT's own
     Opencaselist disclosure for that same round, to compute "win % when
     responding to argument X". This is capped to avoid an unbounded
     fan-out of requests for a single lookup.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.clients.debateland_client import DebateLandClient
from app.clients.opencaselist_client import OpencaselistClient
from app.clients.tabroom_client import TabroomClient
from app.config import Settings
from app.schemas import DataSource, DebaterSummary, RoundResult
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


def _parse_debateland_rounds(team: Dict[str, Any]) -> List[RoundResult]:
    """
    Best-effort parse of a Tournaments.Tech Team/Entry object into
    RoundResult rows. Ref fields (tournament, opponent) may come back as
    plain id strings rather than expanded objects depending on whether the
    /query call included `expand` -- we handle both.
    """
    out: List[RoundResult] = []
    for tr in team.get("tournaments", []) or []:
        tournament_ref = tr.get("tournament")
        tourn_name = tournament_ref.get("name") if isinstance(tournament_ref, dict) else None
        tourn_id_raw = tournament_ref.get("tourn_id") if isinstance(tournament_ref, dict) else None
        try:
            tourn_id = int(tourn_id_raw) if tourn_id_raw is not None else None
        except (TypeError, ValueError):
            tourn_id = None

        for bucket in ("prelim_rounds", "elim_rounds"):
            for rd in tr.get(bucket, []) or []:
                if not isinstance(rd, dict):
                    continue  # unexpanded ref, nothing to parse
                opponent_ref = rd.get("opponent")
                opponent_name = None
                if isinstance(opponent_ref, dict):
                    codes = opponent_ref.get("codes") or []
                    opponent_name = codes[0] if codes else opponent_ref.get("_id")

                out.append(
                    RoundResult(
                        tournament=tourn_name,
                        round_name=rd.get("name") or rd.get("name_std"),
                        side=_normalize_side(rd.get("side")),
                        opponent=opponent_name,
                        result=rd.get("result"),
                        tourn_id=tourn_id,
                        source_note="tournaments.tech",
                    )
                )
    return out


def _record_from_statistics(
    team: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[str]]:
    """Returns (prelim_record_str, elim_record_str, win_pct, overall_record_str)."""
    stats = team.get("statistics") or {}
    prelim = stats.get("prelim_record")
    elim = stats.get("elim_record")
    prelim_str = f"{prelim[0]}-{prelim[1]}" if prelim else None
    elim_str = f"{elim[0]}-{elim[1]}" if elim else None

    wins = (prelim[0] if prelim else 0) + (elim[0] if elim else 0)
    losses = (prelim[1] if prelim else 0) + (elim[1] if elim else 0)
    decided = wins + losses
    if not decided:
        return prelim_str, elim_str, None, None
    win_pct = round(100 * wins / decided, 1)
    overall = f"{wins}-{losses}"
    return prelim_str, elim_str, win_pct, overall


async def _join_argument_data(
    rounds: List[RoundResult], oc_rounds: List[Dict[str, Any]]
) -> None:
    """Mutates `rounds` in place, attaching `.argument` where a matching
    Opencaselist disclosure round is found (matched on tourn_id + side,
    then best-effort on opponent-name substring)."""
    by_tourn_side: Dict[Tuple[Optional[int], Optional[str]], List[Dict[str, Any]]] = {}
    for r in oc_rounds:
        key = (r.get("tourn_id"), _normalize_side(r.get("side")))
        by_tourn_side.setdefault(key, []).append(r)

    for rr in rounds:
        candidates = by_tourn_side.get((rr.tourn_id, rr.side), [])
        if not candidates:
            continue
        if len(candidates) == 1:
            rr.argument = candidates[0].get("report") or None
            continue
        # multiple disclosures at the same tournament/side: narrow by opponent substring
        opp = (rr.opponent or "").lower()
        for c in candidates:
            c_opp = (c.get("opponent") or "").lower()
            if opp and (opp in c_opp or c_opp in opp):
                rr.argument = c.get("report") or None
                break


async def _manual_join(
    settings: Settings, caselist_slug: str, school_hint: str, debater_name: str
) -> List[RoundResult]:
    """Full manual fallback: Opencaselist rounds (source of truth for which
    tournaments/rounds happened + what was read) joined with Tabroom's
    scraped win/loss for each of those rounds via tourn_id/external_id."""
    rounds: List[RoundResult] = []

    async with OpencaselistClient(
        settings.opencaselist_username, settings.opencaselist_password
    ) as oc:
        found = await oc.search_school_and_team(caselist_slug, school_hint, debater_name)
        if not found:
            return rounds
        oc_rounds = found["rounds"]

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
                    source_note="opencaselist + tabroom (manual join)",
                )
            )

    return rounds


async def lookup_debater(
    settings: Settings, query: str, school_hint: Optional[str] = None
) -> DebaterSummary:
    warnings: List[str] = []
    data_sources: List[DataSource] = []
    rounds: List[RoundResult] = []
    matched_name = None
    school = None
    overall_record = None
    win_pct = None
    prelim_record = None
    elim_record = None

    # --- Step 1: Tournaments.Tech (aggregated record) ---
    dl_client = DebateLandClient(settings.debateland_season, settings.debateland_circuit_list)
    team = await dl_client.query_debater(query)

    if team:
        data_sources.append(DataSource.TOURNAMENTS_TECH)
        matched_name = ", ".join(
            c.get("name", "") for c in team.get("competitors", []) if isinstance(c, dict)
        ) or team.get("codes", [None])[0]
        school = (team.get("schools") or [None])[0]
        prelim_record, elim_record, win_pct, overall_record = _record_from_statistics(team)
        rounds = _parse_debateland_rounds(team)
        if not rounds:
            warnings.append(
                "Tournaments.Tech had an aggregate record but round-level detail wasn't "
                "expanded in the response -- win/loss breakdowns by argument may be incomplete."
            )
    else:
        warnings.append("No record found on Tournaments.Tech; falling back to manual Tabroom + Opencaselist join.")

    # --- Step 2: Opencaselist (argument disclosure) ---
    school_for_oc = school_hint or school or query
    try:
        async with OpencaselistClient(
            settings.opencaselist_username, settings.opencaselist_password
        ) as oc:
            found = await oc.search_school_and_team(settings.caselist_slug, school_for_oc, query)
    except Exception:
        found = None

    if found:
        data_sources.append(DataSource.MANUAL_JOIN if not team else DataSource.MIXED)
        if rounds:
            await _join_argument_data(rounds, found["rounds"])
        else:
            # No Tournaments.Tech data at all -- do the full manual join,
            # which also fetches win/loss via Tabroom scraping.
            rounds = await _manual_join(settings, settings.caselist_slug, school_for_oc, query)
    elif not team:
        warnings.append(
            "No disclosure found on Opencaselist either -- this debater may not be disclosing, "
            "may compete on a different circuit, or the name/school didn't match closely enough."
        )

    if not overall_record:
        overall_record, win_pct = stats_service.compute_overall_record(rounds)

    most_read, by_win_pct = stats_service.compute_argument_stats(rounds)

    # --- Step 3 (best effort, capped): argument-faced win rates ---
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
        matched_name=matched_name or query,
        school=school or school_hint,
        overall_record=overall_record,
        win_pct=win_pct,
        prelim_record=prelim_record,
        elim_record=elim_record,
        most_read_arguments=most_read,
        win_pct_by_argument_read=by_win_pct,
        win_pct_vs_argument_faced=win_pct_vs_faced,
        rounds=rounds,
        data_sources=data_sources or [],
        warnings=warnings,
    )
