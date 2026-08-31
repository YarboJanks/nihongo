"""The lesson-plan spine. For the conversational units, GPT generates actual
dialogue/exercises at conversation time — this just defines the progression
and objectives. The kana unit is different: it's fully driven by the
deterministic offline engine in kana_engine.py against
data/kana_curriculum.json, with no live model involved at all.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    id: str
    title: str
    objectives: list[str]
    engine: str = "gpt"  # "gpt" (nihongo lesson) or "kana_offline" (nihongo kana)


UNITS: list[Unit] = [
    Unit(
        id="kana_mastery",
        title="Kana mastery (hiragana, katakana, basic kanji)",
        objectives=[
            "Recognize all hiragana and katakana, including dakuten/handakuten, "
            "yoon combinations, sokuon, and long-vowel conventions",
            "Read common katakana loanwords",
            "Read ~15 basic kanji in context",
        ],
        engine="kana_offline",
    ),
    Unit(
        id="basic_grammar",
        title="Basic grammar",
        objectives=[
            "Use です/ます polite forms",
            "Use は, が, を particles correctly",
            "Form simple present-tense sentences",
        ],
    ),
    Unit(
        id="daily_life",
        title="Daily life conversation",
        objectives=[
            "Talk about daily routines, food, and hobbies",
            "Introduce ~20 common kanji in context alongside kana readings",
        ],
    ),
    Unit(
        id="conversation",
        title="Extended conversation practice",
        objectives=[
            "Hold multi-turn conversations on everyday topics",
            "Read short passages mixing kana and kanji",
        ],
    ),
]

_BY_ID = {u.id: u for u in UNITS}


def get_unit(unit_id: str) -> Unit:
    return _BY_ID[unit_id]


def first_unit() -> Unit:
    return UNITS[0]


def next_unit(current_id: str) -> Unit | None:
    ids = [u.id for u in UNITS]
    idx = ids.index(current_id)
    if idx + 1 < len(UNITS):
        return UNITS[idx + 1]
    return None
