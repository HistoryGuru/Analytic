"""
Extracts the actual argument(s) a debater read/extended in a round from
Opencaselist's raw free-text `report` field -- which is NOT the argument
itself, just a disclosure blob that mixes labels and case names together,
e.g.:

    "1AC\\nLynfinity and Beyond\\n2NR\\nT-Subsets"
    "1AC\\nNEW Yellow Peril\\n2NR\\nAI DA\\nBallot CP\\nT-Framework"

The extraction rule (confirmed with the user against real examples):
  - If the debater was AFF that round: the argument is whatever follows
    the "1AC" label.
  - If the debater was NEG that round: prefer whatever follows a "2NR"
    label (what they actually extended/went for -- the more meaningful
    signal than everything listed in the 1NC, which is often several
    throwaway off-case positions). If there's no "2NR" label in the text,
    fall back to whatever follows "1NC".
  - A round can extend multiple positions (e.g. "AI DA, Ballot CP,
    T-Framework" all in one 2NR) -- each counts as its own argument, not
    one combined blob.

Free-text formatting varies a lot debater-to-debater (comma vs newline
separated, inconsistent capitalization, "1NC" vs "2NR" as the meaningful
label, stray whitespace) -- regex was tried and kept breaking on edge
cases, so this is an LLM extraction task: one batched Groq call handles
every round for a debater at once, rather than one call per round.
"""
import json
import re
from typing import List, Optional

from groq import AsyncGroq

SYSTEM_PROMPT = """You extract debate argument names from messy free-text disclosure \
notes. You will be given a numbered list of rounds, each with the side the debater \
was on (Aff or Neg) and the raw disclosure text for that round.

Rule for each round:
- If side is "Aff": extract the argument/case name that follows the "1AC" label in \
the text (stop before the next label, e.g. "2NR", "2AR", "1AR").
- If side is "Neg": prefer the argument name(s) that follow a "2NR" label (what they \
actually extended/went for). If the text has no "2NR" label at all, use whatever \
follows "1NC" instead.
- A round can have MULTIPLE arguments (comma or newline separated after the label) -- \
list each one separately, not combined into one string.
- Keep names short and as-written (e.g. "AI DA", "Ballot CP", "T-Framework", "Yellow \
Peril") -- don't paraphrase or expand them.
- If the relevant label isn't present in the text at all, return an empty list for \
that round.

Respond with ONLY a JSON array, no other text, in this exact shape:
[{"index": 0, "arguments": ["Yellow Peril"]}, {"index": 1, "arguments": ["AI DA", "Ballot CP", "T-Framework"]}]
"""


def _build_user_prompt(sides: List[str], raw_reports: List[str]) -> str:
    lines = []
    for i, (side, report) in enumerate(zip(sides, raw_reports)):
        cleaned = (report or "").strip() or "(empty)"
        lines.append(f"Round {i}: side={side or 'Unknown'}\n{cleaned}\n")
    return "\n".join(lines)


def _parse_json_response(text: str) -> Optional[list]:
    text = text.strip()
    # Strip markdown code fences if the model added them despite instructions.
    text = re.sub(r"^```(json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None


async def extract_arguments_batch(
    sides: List[str], raw_reports: List[str], api_key: str, model: str
) -> List[List[str]]:
    """
    Given parallel lists of (side, raw_report) for a debater's rounds,
    returns a parallel list of extracted argument lists -- rounds[i]'s
    arguments are the result of extracting from raw_reports[i] given
    sides[i]. Never raises: on any failure (bad key, malformed JSON,
    network issue), returns all-empty lists so callers degrade gracefully
    to "no argument stats" rather than crashing the whole lookup.
    """
    if not raw_reports:
        return []

    empty_result = [[] for _ in raw_reports]

    try:
        client = AsyncGroq(api_key=api_key)
        user_prompt = _build_user_prompt(sides, raw_reports)

        completion = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=2000,
        )
        text = completion.choices[0].message.content or ""
        parsed = _parse_json_response(text)
        if not isinstance(parsed, list):
            return empty_result

        results = list(empty_result)  # copy
        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            args = item.get("arguments")
            if isinstance(idx, int) and 0 <= idx < len(results) and isinstance(args, list):
                results[idx] = [str(a).strip() for a in args if str(a).strip()]
        return results

    except Exception:
        return empty_result
