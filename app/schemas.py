"""Pydantic models shared across the app -- these define the shape of the
final API response, independent of which upstream source produced the data."""
from typing import List, Optional

from pydantic import BaseModel


class RoundResult(BaseModel):
    tournament: Optional[str] = None
    round_name: Optional[str] = None
    side: Optional[str] = None          # "Aff" / "Neg" (normalized)
    opponent: Optional[str] = None
    judge: Optional[str] = None
    result: Optional[str] = None        # "Win" / "Loss" / "Bye" / None if unknown
    arguments: List[str] = []           # extracted argument(s) actually read/extended this round
    raw_report: Optional[str] = None    # the raw Opencaselist disclosure text, before extraction
    source_note: Optional[str] = None   # brief provenance, e.g. "opencaselist + tabroom"


class ArgumentStat(BaseModel):
    argument: str
    times_read: int
    wins: int
    losses: int
    win_pct: Optional[float] = None


class OpponentArgumentStat(BaseModel):
    """Win rate when the debater FACED a given argument (i.e. had to answer it)."""
    argument: str
    times_faced: int
    wins: int
    losses: int
    win_pct: Optional[float] = None


class DebaterSummary(BaseModel):
    query: str
    matched_name: Optional[str] = None
    school: Optional[str] = None
    code: Optional[str] = None
    event: str = "Lincoln Douglas"
    season: Optional[str] = None

    overall_record: Optional[str] = None     # e.g. "24-8"
    win_pct: Optional[float] = None
    prelim_record: Optional[str] = None
    elim_record: Optional[str] = None

    most_read_arguments: List[ArgumentStat] = []
    win_pct_by_argument_read: List[ArgumentStat] = []
    win_pct_vs_argument_faced: List[OpponentArgumentStat] = []

    rounds: List[RoundResult] = []

    data_sources: List[str] = []         # e.g. ["opencaselist", "tabroom"]
    warnings: List[str] = []             # e.g. "No school provided -- results may be unreliable"


class StrategyNotes(BaseModel):
    summary: str
    suggested_strategy: str
    model_used: str


class DebaterReport(BaseModel):
    debater: DebaterSummary
    notes: Optional[StrategyNotes] = None
