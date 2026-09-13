"""
Generates human-readable scouting notes and a suggested strategy using a
Groq-hosted open model (fast + generous free tier). Uses the official
`groq` Python SDK, which is OpenAI-compatible under the hood.
"""
from groq import AsyncGroq

from app.schemas import DebaterSummary, StrategyNotes

SYSTEM_PROMPT = """You are an experienced Lincoln-Douglas debate coach helping a \
student prepare against an opponent. You are given structured scouting data \
about that opponent: their overall record, which arguments/cases they read \
most often, their win rate with those arguments, and (when available) their \
win rate against arguments they've had to answer. Write two things:

1. A concise scouting summary (3-5 sentences) describing this debater's \
tendencies -- what they read, how effective it's been, any patterns.
2. A concrete suggested strategy (4-8 sentences or bullet points) for how to \
prepare against them: what to prep out, what pressure points their read \
arguments have historically shown, and any strategic angles suggested by \
the win-rate data.

Be specific and reference the actual argument names and numbers you were \
given. If the data is sparse, say so plainly rather than inventing detail. \
Do not fabricate arguments, records, or tournaments beyond what's provided."""


def _build_user_prompt(debater: DebaterSummary) -> str:
    lines = [f"Debater: {debater.matched_name or debater.query}"]
    if debater.school:
        lines.append(f"School: {debater.school}")
    if debater.overall_record:
        lines.append(f"Overall record: {debater.overall_record} ({debater.win_pct}% win rate)")
    if debater.prelim_record:
        lines.append(f"Prelim record: {debater.prelim_record}")
    if debater.elim_record:
        lines.append(f"Elim record: {debater.elim_record}")

    if debater.most_read_arguments:
        lines.append("\nMost-read arguments:")
        for a in debater.most_read_arguments[:8]:
            wl = f", {a.win_pct}% win rate" if a.win_pct is not None else ""
            lines.append(f"- {a.argument}: read {a.times_read}x ({a.wins}-{a.losses}{wl})")

    if debater.win_pct_vs_argument_faced:
        lines.append("\nWin rate when FACING these arguments (i.e. their track record answering them):")
        for a in debater.win_pct_vs_argument_faced[:8]:
            wl = f", {a.win_pct}% win rate" if a.win_pct is not None else ""
            lines.append(f"- {a.argument}: faced {a.times_faced}x ({a.wins}-{a.losses}{wl})")

    if debater.warnings:
        lines.append("\nData caveats: " + "; ".join(debater.warnings))

    return "\n".join(lines)


async def generate_strategy_notes(
    debater: DebaterSummary, api_key: str, model: str
) -> StrategyNotes:
    client = AsyncGroq(api_key=api_key)
    user_prompt = _build_user_prompt(debater)

    completion = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=700,
    )

    text = completion.choices[0].message.content or ""

    # Split into summary/strategy halves on a best-effort basis; if the
    # model doesn't clearly separate them, just put everything in both
    # fields' natural home (summary gets the full text, strategy stays
    # empty) rather than mangling it with regex guesses.
    summary, _, strategy = text.partition("\n\n")
    if not strategy:
        summary, strategy = text, ""

    return StrategyNotes(summary=summary.strip(), suggested_strategy=strategy.strip(), model_used=model)
