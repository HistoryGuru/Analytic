"""
Orchestrates a single debater lookup end-to-end.

Rewritten after confirming (via live browser inspection, not guessing) how
Tabroom's data actually looks:
  - Opencaselist's Round.tourn_id / Round.external_id are always null in
    practice for this caselist -- the original "join by ID" plan doesn't
    work. Abandoned.
  - Tabroom's tournament entries list (fields.mhtml?tourn_id=X) is a plain
    HTML table with a link containing the entry's persistent id (id1).
  - Tabroom's team_results.mhtml?id1=X returns that entry's ENTIRE current
    season -- every tournament, every round -- in a single request. So we
    only need to resolve id1 ONCE per debater (via any one of their
    tournaments), not per round.

Flow:
  1. Resolve the debater's school + team + disclosed rounds on Opencaselist
     (source of "what was read," and the list of tournament names to try).
  2. Try each distinct tournament name from step 1 against Tabroom
     (search_tournament -> find_entry_id) until one resolves to an entry id.
  3. Fetch that entry's whole-season round log from Tabroom in one call.
  4. Join Opencaselist rounds (has the argument) with Tabroom rounds (has
     the win/loss) by tournament name (fuzzy) + round name + side.
  5. Best-effort, capped: resolve a handful of opponents' own Opencaselist
     disclosure for the same rounds (matched the same way, by tournament +
     round name), to compute "win % facing argument X".
  6. The `report` field from Opencaselist is NOT the argument itself -- it's
     raw disclosure text mixing labels ("1AC", "2NR", "1NC") with case
     names, and the meaningful part depends on which side the debater was
     on that round. Extraction of the actual argument(s) happens via one
     batched Groq call per debater (see argument_extraction.py) rather than
     regex, since the free-text formatting varies too much to parse
     reliably by hand.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

from app.clients.opencaselist_client import OpencaselistClient
from app.clients.tabroom_client import TabroomClient, clean_tournament_name
from app.config import Settings
from app.schemas import DebaterSummary, RoundResult
from app.services import stats as stats_service
from app.services.argument_extraction import extract_arguments_batch

_MAX_OPPONENT_LOOKUPS = 12  # cap on "argument faced" resolution per query
_MAX_TOURNAMENT_RESOLVE_ATTEMPTS = 5  # how many distinct tournament names to try before giving up


def _normalize_side(side: Optional[str]) -> Optional[str]:
    if not side:
        return None
    s = side.strip().lower()
    if s in ("a", "aff", "pro"):
        return "Aff"
    if s in ("n", "neg", "con"):
        return "Neg"
    return side


def _tournaments_match(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    a_norm, b_norm = a.strip().lower(), b.strip().lower()
    if a_norm in b_norm or b_norm in a_norm:
        return True
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= 0.6


async def _resolve_rounds(
    settings: Settings, school_hint: str, debater_name: str
) -> Tuple[Optional[str], List[RoundResult], List[str]]:
    """Returns (school_slug_matched, rounds, diagnostics). `diagnostics`
    pinpoints exactly which step failed instead of a single opaque
    "not found.\""""
    rounds: List[RoundResult] = []
    diagnostics: List[str] = []

    async with OpencaselistClient(
        settings.opencaselist_username, settings.opencaselist_password
    ) as oc:
        school_slug = await oc.find_school(settings.caselist_slug, school_hint)
        if not school_slug:
            suggestions = await oc.suggest_schools(settings.caselist_slug, school_hint)
            if suggestions:
                diagnostics.append(
                    f"No school on Opencaselist matched '{school_hint}' exactly. "
                    f"Closest listed names: {', '.join(suggestions)}."
                )
            else:
                diagnostics.append(
                    f"No school on Opencaselist resembles '{school_hint}' at all -- double-check "
                    f"CASELIST_SLUG ('{settings.caselist_slug}') is the right slug for this season."
                )
            return None, rounds, diagnostics

        team = await oc.find_team(settings.caselist_slug, school_slug, debater_name)
        if not team:
            diagnostics.append(
                f"Found '{school_slug}' on Opencaselist, but no team there matches the name "
                f"'{debater_name}' -- they may not be disclosing, may be listed under a partner's "
                f"name, or the name doesn't match how Opencaselist has it spelled."
            )
            return school_slug, rounds, diagnostics

        oc_rounds = await oc.get_team_rounds(settings.caselist_slug, school_slug, team["name"])
        if not oc_rounds:
            diagnostics.append(
                f"Found the team at '{school_slug}' on Opencaselist, but they haven't disclosed "
                f"any rounds yet this season."
            )
            return school_slug, rounds, diagnostics

    # Opencaselist wiki pages often have a "00---Contact Info" pseudo-round
    # at the top for listing team contact info -- not a real competition
    # round. Filter these out before they waste a tournament-resolution
    # attempt or pollute the round log. Recognized by tournament name
    # containing "contact info", or the structural marker seen on real
    # examples: opponent/judge both literally "Cites".
    def _is_real_round(r: Dict[str, Any]) -> bool:
        tourn = (r.get("tournament") or "").lower()
        if "contact info" in tourn:
            return False
        if r.get("opponent") == "Cites" and r.get("judge") == "Cites":
            return False
        return True

    oc_rounds = [r for r in oc_rounds if _is_real_round(r)]
    if not oc_rounds:
        diagnostics.append(
            f"Found the team at '{school_slug}' on Opencaselist, but every disclosed entry was a "
            f"non-round placeholder (e.g. a contact-info page), not an actual competition round."
        )
        return school_slug, rounds, diagnostics

    # A canonical name straight from Opencaselist's own team record is
    # often more reliable for matching Tabroom's entry list than whatever
    # the user typed (different capitalization, nicknames, etc.) -- try
    # both, since either could be the one that actually matches.
    name_candidates = [debater_name]
    canonical_name = " ".join(filter(None, [team.get("debater1_first"), team.get("debater1_last")])).strip()
    if canonical_name and canonical_name.lower() != debater_name.strip().lower():
        name_candidates.append(canonical_name)

    # Distinct tournament names, in the order they appear, to try resolving
    # against Tabroom -- we only need ONE to succeed, since team_results
    # gives us the whole season once we have an entry id.
    distinct_tournaments: List[str] = []
    for r in oc_rounds:
        t = r.get("tournament")
        if t and t not in distinct_tournaments:
            distinct_tournaments.append(t)

    tabroom_rounds: List[Dict[str, Any]] = []
    entry_id: Optional[int] = None
    attempt_log: List[str] = []
    async with TabroomClient(settings.tabroom_username, settings.tabroom_password) as tb:
        if not tb.authenticated:
            diagnostics.append(
                "Tabroom login didn't return an auth cookie -- check TABROOM_USERNAME/TABROOM_PASSWORD "
                "(this alone would make every tournament-resolution attempt below fail)."
            )

        for tourn_name in distinct_tournaments[:_MAX_TOURNAMENT_RESOLVE_ATTEMPTS]:
            clean = clean_tournament_name(tourn_name)
            tourn_id = await tb.search_tournament(tourn_name)
            if not tourn_id:
                attempt_log.append(f"'{clean}': no matching tournament found via Tabroom search")
                continue

            found_id = None
            for name_candidate in name_candidates:
                found_id = await tb.find_entry_id(tourn_id, school_slug, name_candidate)
                if found_id:
                    break

            if found_id:
                entry_id = found_id
                break
            else:
                tried_names = " / ".join(f"'{n}'" for n in name_candidates)
                attempt_log.append(
                    f"'{clean}': found tourn_id={tourn_id}, but no entry row at school='{school_slug}' "
                    f"matched name {tried_names}"
                )

        if entry_id:
            tabroom_rounds = await tb.get_season_rounds(entry_id)
        else:
            diagnostics.append(
                f"Found {len(oc_rounds)} disclosed round(s) on Opencaselist, but couldn't resolve a "
                f"Tabroom entry: " + "; ".join(attempt_log)
            )

    # Join: Opencaselist round (has the argument) + Tabroom round (has the result)
    for r in oc_rounds:
        oc_side = _normalize_side(r.get("side"))
        oc_round_name = (r.get("round") or "").strip().lower()
        oc_tourn_clean = clean_tournament_name(r.get("tournament") or "")

        match = None
        for tr in tabroom_rounds:
            if (
                _tournaments_match(oc_tourn_clean, tr.get("tournament"))
                and tr.get("round", "").strip().lower() == oc_round_name
                and _normalize_side(tr.get("side")) == oc_side
            ):
                match = tr
                break

        rounds.append(
            RoundResult(
                tournament=r.get("tournament"),
                round_name=r.get("round"),
                side=oc_side,
                opponent=(match or {}).get("opponent") or r.get("opponent"),
                judge=(match or {}).get("judge") or r.get("judge"),
                result=(match or {}).get("result"),
                raw_report=r.get("report"),
                source_note="opencaselist + tabroom" if match else "opencaselist only (no tabroom match)",
            )
        )

    return school_slug, rounds, diagnostics


def _opposite_side(side: Optional[str]) -> Optional[str]:
    if side == "Aff":
        return "Neg"
    if side == "Neg":
        return "Aff"
    return None


async def _find_opponent_raw_report(
    settings: Settings, opponent_label: str, tournament: Optional[str], round_name: Optional[str]
) -> Optional[str]:
    """Best-effort: treat the opponent's Tabroom-style label (e.g.
    'Harvard-Westlake SL') as both a school and name guess on Opencaselist,
    and pull whatever raw text they disclosed for the same tournament +
    round -- extraction happens later, batched, in lookup_debater."""
    async with OpencaselistClient(
        settings.opencaselist_username, settings.opencaselist_password
    ) as oc:
        found = await oc.search_school_and_team(settings.caselist_slug, opponent_label, opponent_label)
        if not found:
            return None
        tourn_clean = clean_tournament_name(tournament or "")
        round_norm = (round_name or "").strip().lower()
        for oc_round in found["rounds"]:
            if (
                _tournaments_match(tourn_clean, clean_tournament_name(oc_round.get("tournament") or ""))
                and (oc_round.get("round") or "").strip().lower() == round_norm
            ):
                return oc_round.get("report")
    return None


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
        school_matched, rounds, resolve_diagnostics = await _resolve_rounds(settings, school_for_oc, query)
        warnings.extend(resolve_diagnostics)
    except Exception as exc:
        rounds = []
        school_matched = None
        warnings.append(f"Lookup failed while contacting Opencaselist or Tabroom: {exc}")

    if rounds:
        data_sources.append("opencaselist")
        if any(r.result for r in rounds):
            data_sources.append("tabroom")

    # --- Extract actual arguments from raw disclosure text (batched, one Groq call) ---
    if rounds and settings.groq_api_key:
        try:
            extracted = await extract_arguments_batch(
                [r.side for r in rounds],
                [r.raw_report for r in rounds],
                settings.groq_api_key,
                settings.groq_model,
            )
            for r, args in zip(rounds, extracted):
                r.arguments = args
            if not any(r.arguments for r in rounds):
                warnings.append(
                    "Couldn't extract specific arguments from disclosure text this time -- "
                    "argument stats may be empty even though rounds were found."
                )
        except Exception as exc:
            warnings.append(f"Argument extraction failed ({exc}) -- showing record without argument stats.")
    elif rounds and not settings.groq_api_key:
        warnings.append("GROQ_API_KEY not configured -- skipped argument extraction (no argument stats).")

    overall_record, win_pct = stats_service.compute_overall_record(rounds)
    most_read, by_win_pct = stats_service.compute_argument_stats(rounds)

    # --- Best effort, capped: argument-faced win rates ---
    faced_pairs: List[Tuple[str, Optional[str]]] = []
    rounds_with_opponents = [r for r in rounds if r.opponent and r.result][:_MAX_OPPONENT_LOOKUPS]
    if rounds_with_opponents and settings.groq_api_key:
        try:
            opponent_sides = [_opposite_side(r.side) for r in rounds_with_opponents]
            opponent_reports = []
            for r in rounds_with_opponents:
                raw = await _find_opponent_raw_report(settings, r.opponent or "", r.tournament, r.round_name)
                opponent_reports.append(raw)

            extracted_opponent_args = await extract_arguments_batch(
                opponent_sides, opponent_reports, settings.groq_api_key, settings.groq_model
            )
            for r, args in zip(rounds_with_opponents, extracted_opponent_args):
                for arg in args:
                    faced_pairs.append((arg, r.result))
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
