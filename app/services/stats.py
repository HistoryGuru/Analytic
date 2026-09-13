"""
Turns a flat list of joined round results into the aggregate stats the
frontend/AI notes care about: overall win %, most-read arguments, win %
when reading a given argument, and win % when facing a given argument.

Argument names in disclosure ("report" fields on Opencaselist, or a
tournament's own case-tag field) are free text -- the same case gets
written as "Util AC", "Utilitarianism AC", "UTIL", etc. We do lightweight
fuzzy clustering (difflib) rather than exact string matching so these get
grouped together. This is intentionally simple/transparent rather than an
ML approach, since debate case names are short and the vocabulary is
fairly consistent within a single debater's own disclosure.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from app.schemas import ArgumentStat, OpponentArgumentStat, RoundResult

_SIMILARITY_THRESHOLD = 0.82


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(ac|nc|1ac|1nc|aff|neg|case)\b", "", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cluster_arguments(raw_names: List[str]) -> Dict[str, str]:
    """Map each raw argument string -> a canonical cluster label
    (the most common raw form within that cluster)."""
    normalized_to_raws: Dict[str, List[str]] = defaultdict(list)
    for raw in raw_names:
        normalized_to_raws[_normalize(raw)].append(raw)

    keys = list(normalized_to_raws.keys())
    clusters: List[List[str]] = []
    assigned: Dict[str, int] = {}

    for key in keys:
        if not key:
            continue
        best_idx: Optional[int] = None
        best_ratio = 0.0
        for idx, cluster in enumerate(clusters):
            rep = cluster[0]
            ratio = difflib.SequenceMatcher(None, key, rep).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = idx
        if best_idx is not None and best_ratio >= _SIMILARITY_THRESHOLD:
            clusters[best_idx].append(key)
            assigned[key] = best_idx
        else:
            clusters.append([key])
            assigned[key] = len(clusters) - 1

    # Pick a display label per cluster = the most frequent raw string in it.
    cluster_labels: Dict[int, str] = {}
    for idx, cluster_keys in enumerate(clusters):
        raws_in_cluster: List[str] = []
        for k in cluster_keys:
            raws_in_cluster.extend(normalized_to_raws[k])
        # most common raw string, longest as tiebreak (usually more descriptive)
        label = max(set(raws_in_cluster), key=lambda r: (raws_in_cluster.count(r), len(r)))
        cluster_labels[idx] = label.strip()

    raw_to_label: Dict[str, str] = {}
    for key, raws in normalized_to_raws.items():
        if not key:
            for r in raws:
                raw_to_label[r] = r.strip()
            continue
        idx = assigned[key]
        for r in raws:
            raw_to_label[r] = cluster_labels[idx]

    return raw_to_label


def compute_argument_stats(rounds: List[RoundResult]) -> Tuple[List[ArgumentStat], List[ArgumentStat]]:
    """Returns (most_read_arguments, win_pct_by_argument_read), both sorted
    descending by times_read."""
    disclosed = [r for r in rounds if r.argument]
    if not disclosed:
        return [], []

    label_map = cluster_arguments([r.argument for r in disclosed])  # type: ignore[arg-type]

    tally: Dict[str, Dict[str, int]] = defaultdict(lambda: {"read": 0, "win": 0, "loss": 0})
    for r in disclosed:
        label = label_map[r.argument]  # type: ignore[index]
        tally[label]["read"] += 1
        if r.result == "Win":
            tally[label]["win"] += 1
        elif r.result == "Loss":
            tally[label]["loss"] += 1

    stats: List[ArgumentStat] = []
    for label, counts in tally.items():
        decided = counts["win"] + counts["loss"]
        win_pct = round(100 * counts["win"] / decided, 1) if decided else None
        stats.append(
            ArgumentStat(
                argument=label,
                times_read=counts["read"],
                wins=counts["win"],
                losses=counts["loss"],
                win_pct=win_pct,
            )
        )

    most_read = sorted(stats, key=lambda s: s.times_read, reverse=True)
    by_win_pct = sorted(
        [s for s in stats if s.win_pct is not None],
        key=lambda s: s.win_pct,  # type: ignore[arg-type,return-value]
        reverse=True,
    )
    return most_read, by_win_pct


def compute_opponent_argument_stats(
    faced_arguments: List[Tuple[str, Optional[str]]]
) -> List[OpponentArgumentStat]:
    """
    faced_arguments: list of (argument_the_debater_faced, result) tuples,
    where result is the debater's own result ("Win"/"Loss") in that round.
    This requires knowing what the OPPONENT disclosed in the same round --
    see services/lookup.py for how that gets assembled.
    """
    valid = [(a, r) for a, r in faced_arguments if a]
    if not valid:
        return []

    label_map = cluster_arguments([a for a, _ in valid])
    tally: Dict[str, Dict[str, int]] = defaultdict(lambda: {"faced": 0, "win": 0, "loss": 0})
    for arg, result in valid:
        label = label_map[arg]
        tally[label]["faced"] += 1
        if result == "Win":
            tally[label]["win"] += 1
        elif result == "Loss":
            tally[label]["loss"] += 1

    stats = []
    for label, counts in tally.items():
        decided = counts["win"] + counts["loss"]
        win_pct = round(100 * counts["win"] / decided, 1) if decided else None
        stats.append(
            OpponentArgumentStat(
                argument=label,
                times_faced=counts["faced"],
                wins=counts["win"],
                losses=counts["loss"],
                win_pct=win_pct,
            )
        )
    return sorted(stats, key=lambda s: s.times_faced, reverse=True)


def compute_overall_record(rounds: List[RoundResult]) -> Tuple[Optional[str], Optional[float]]:
    wins = sum(1 for r in rounds if r.result == "Win")
    losses = sum(1 for r in rounds if r.result == "Loss")
    decided = wins + losses
    if decided == 0:
        return None, None
    return f"{wins}-{losses}", round(100 * wins / decided, 1)
