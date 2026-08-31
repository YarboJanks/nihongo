"""OpenAI-backed conversational tutor for `nihongo lesson`.

Only used for the post-kana curriculum units (basic_grammar, daily_life,
conversation) — kana teaching is fully offline now, via kana_engine.py.
Structured progress (new vocab/grammar, unit mastery) is captured via tool
calls rather than by parsing the chat transcript, so it survives even though
the conversation itself isn't persisted.
"""

import os

from openai import OpenAI
from rich.console import Console

from . import curriculum, db

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "record_vocab",
            "description": (
                "Record a new vocabulary word, grammar point, or phrase the "
                "learner has just been taught or practiced, so it enters "
                "their spaced-repetition review queue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_type": {"type": "string", "enum": ["vocab", "grammar"]},
                    "word": {"type": "string", "description": "The Japanese word/phrase"},
                    "reading": {"type": "string", "description": "Reading in kana"},
                    "meaning": {"type": "string", "description": "English meaning"},
                },
                "required": ["item_type", "word", "reading", "meaning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_unit",
            "description": (
                "Mark the current curriculum unit complete because the "
                "learner has reliably demonstrated its objectives, advancing "
                "them to the next unit. Only call this when recognition or "
                "mastery is reliable, not merely after exposure."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def build_system_prompt(conn, unit: curriculum.Unit) -> str:
    parts = [
        "You are a patient, encouraging Japanese language tutor running "
        "inside a command-line app. The learner already went through a "
        "dedicated offline kana-reading course before reaching this unit, so "
        "they can read hiragana/katakana and a small set of basic kanji — "
        "write in real Japanese text, not romaji, except to confirm a new "
        "or unusual reading.",
        f"\nCurrent curriculum unit: {unit.title}",
        "Objectives for this unit:\n" + "\n".join(f"- {o}" for o in unit.objectives),
    ]

    parts.append(
        "\nCall record_vocab whenever you introduce or practice a new "
        "word, phrase, or grammar point, so it enters spaced-repetition "
        "review."
    )

    parts.append(
        "\nWhen the learner has reliably demonstrated this unit's "
        "objectives — not just been exposed to them — call complete_unit."
    )
    parts.append(
        "\nYou own pacing and sequencing, not the learner. Using the "
        "progress data above (weak characters, due reviews, what's not yet "
        "introduced), decide yourself what to teach, drill, read, or review "
        "next and move directly into it — don't ask the learner what they "
        "want to do next or offer them a menu of options. It's fine to "
        "narrate the plan in one short line ('let's drill your weak "
        "characters, then read a new word'), but then act on it immediately "
        "rather than waiting for them to choose. Only pause to genuinely "
        "wait for the learner when you need their actual answer to a drill, "
        "quiz, or question you just asked."
    )
    parts.append(
        "\nNEVER reveal the reading/romaji/meaning of a character or word in "
        "the same message where you're freshly quizzing the learner on it — "
        "that defeats the quiz. Ask blind ('What is this character: き?') "
        "and wait for their answer in a separate turn before confirming or "
        "revealing anything. It's fine to reveal an answer immediately after "
        "they respond, in the message that follows their answer."
    )
    parts.append(
        "\nWhen you call a tool (review_kana, record_vocab, complete_unit), "
        "always also include your reply text in that same turn if you "
        "already know what you want to say — don't make an empty tool-only "
        "turn if you can say your next line at the same time. This keeps "
        "response latency down."
    )
    parts.append(
        "\nKeep turns conversational and reasonably short. Use real Japanese "
        "text (kana/kanji) rather than romaji wherever the unit's objectives "
        "call for reading practice; romaji may confirm pronunciation but "
        "should fade out as the learner progresses."
    )
    return "\n".join(parts)


def _execute_tool(conn, name: str, args: dict) -> tuple[str, str | None]:
    """Returns (tool_result_text, banner_message_or_None)."""
    if name == "record_vocab":
        db.upsert_srs_item(
            conn,
            item_type=args["item_type"],
            prompt=args["word"],
            answer=args["reading"],
            meaning=args["meaning"],
        )
        return "ok", None

    if name == "complete_unit":
        new_unit_id = db.complete_current_unit(conn)
        if new_unit_id is None:
            return "ok", "🎉 You've completed the entire curriculum!"
        return "ok", f"✅ Unit complete! Moving on to: {curriculum.get_unit(new_unit_id).title}"

    return f"Unknown tool: {name}", None


def run_lesson(conn, console: Console) -> None:
    unit = curriculum.get_unit(db.get_progress(conn)["unit_id"])
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    client = OpenAI()

    messages = [{"role": "system", "content": build_system_prompt(conn, unit)}]
    session_id = db.start_session(conn, "lesson", unit.id)

    # Kick the model off so it starts teaching without the learner needing to
    # type a greeting first.
    messages.append({"role": "user", "content": "Let's begin."})

    import json

    # The whole conversation runs in the terminal's alternate screen buffer,
    # like vim/htop — nothing from it ever lands in your normal scrollback.
    # When the `with` block exits, the terminal snaps back to whatever was
    # on screen before the lesson started.
    try:
        with console.screen(hide_cursor=False):
            while True:
                response = client.chat.completions.create(
                    model=model, messages=messages, tools=TOOLS
                )
                msg = response.choices[0].message
                messages.append(msg.model_dump(exclude_none=True))

                # Only round-trip again if the model left us nothing to show —
                # if it already gave content alongside its tool calls, that's
                # one request for the whole turn instead of two.
                while msg.tool_calls and not msg.content:
                    for call in msg.tool_calls:
                        args = json.loads(call.function.arguments or "{}")
                        result, banner = _execute_tool(conn, call.function.name, args)
                        if banner:
                            console.print(f"\n[bold green]{banner}[/bold green]\n")
                            unit = curriculum.get_unit(db.get_progress(conn)["unit_id"])
                        messages.append(
                            {"role": "tool", "tool_call_id": call.id, "content": result}
                        )
                    response = client.chat.completions.create(
                        model=model, messages=messages, tools=TOOLS
                    )
                    msg = response.choices[0].message
                    messages.append(msg.model_dump(exclude_none=True))

                # If content arrived together with tool_calls, the tool_calls
                # above were never executed yet in that branch — handle them now.
                if msg.tool_calls:
                    for call in msg.tool_calls:
                        args = json.loads(call.function.arguments or "{}")
                        result, banner = _execute_tool(conn, call.function.name, args)
                        if banner:
                            console.print(f"\n[bold green]{banner}[/bold green]\n")
                            unit = curriculum.get_unit(db.get_progress(conn)["unit_id"])
                        messages.append(
                            {"role": "tool", "tool_call_id": call.id, "content": result}
                        )

                console.clear()
                console.print(f"[bold cyan]{unit.title}[/bold cyan] [dim](/quit to end)[/dim]\n")
                if msg.content:
                    console.print(f"[bold magenta]先生:[/bold magenta] {msg.content}\n")

                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                except EOFError:
                    break
                if user_input.strip().lower() in ("/quit", "/exit"):
                    break
                messages.append({"role": "user", "content": user_input})
    finally:
        db.end_session(conn, session_id)
        console.print("\n[dim]Lesson saved. See you next time — run `nihongo` anytime to pick up where you left off.[/dim]")
