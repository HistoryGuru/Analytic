from fastapi import APIRouter, HTTPException, Query

from app.cache import cache_key, lookup_cache
from app.config import get_settings
from app.schemas import DebaterReport
from app.services.ai_notes import generate_strategy_notes
from app.services.lookup import lookup_debater

router = APIRouter(prefix="/api/debater", tags=["debater"])


@router.get("", response_model=DebaterReport)
async def get_debater_report(
    q: str = Query(..., min_length=2, description="Debater name or school/code, e.g. 'Jane Doe' or 'Harvard-Westlake AB'"),
    school: str | None = Query(None, description="Optional school name/code hint to disambiguate"),
    notes: bool = Query(True, description="Whether to generate AI strategy notes (set false to skip the Groq call)"),
):
    settings = get_settings()
    key = cache_key("debater", q, school or "")

    if key in lookup_cache:
        summary = lookup_cache[key]
    else:
        summary = await lookup_debater(settings, q, school_hint=school)
        lookup_cache[key] = summary

    if not summary.rounds and not summary.overall_record:
        raise HTTPException(
            status_code=404,
            detail="Couldn't find this debater on Opencaselist. Opencaselist is organized by "
            "school, so a `school` param matching their team's listed school is usually required "
            "-- try the ?school= query param, or double-check spelling of both name and school.",
        )

    strategy_notes = None
    if notes and settings.groq_api_key:
        try:
            strategy_notes = await generate_strategy_notes(summary, settings.groq_api_key, settings.groq_model)
        except Exception as exc:
            summary.warnings.append(f"AI strategy notes failed ({exc}) -- showing the record/stats without them.")
    elif notes and not settings.groq_api_key:
        summary.warnings.append("GROQ_API_KEY not configured -- skipped AI strategy notes.")

    return DebaterReport(debater=summary, notes=strategy_notes)
