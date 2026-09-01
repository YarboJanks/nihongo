"""Deterministic, offline kana-teaching engine.

Drives `nihongo kana` entirely from data/kana_curriculum.json — a batch list,
plus an advancement rule, a weak-character prioritization rule, and a session
sequence, all authored externally (by the learner, via ChatGPT) as an exact,
implementable spec. No API calls happen anywhere in this module; everything
here is plain deterministic logic over locally stored attempt history.
"""

import json
import random
import time
import unicodedata
from datetime import date
from pathlib import Path

from rich.console import Console

from . import db

_DATA_PATH = Path(__file__).parent / "data" / "kana_curriculum.json"
_DATA = json.loads(_DATA_PATH.read_text())
BATCHES = _DATA["batches"]
BATCHES_BY_ID = {b["id"]: b for b in BATCHES}
BATCH_ORDER = [b["id"] for b in BATCHES]

REVIEW_COUNT = 10
DRILL_COUNT = 20
DRILL_CURRENT = 12
DRILL_WEAK = 6
DRILL_SPACED = 2
READING_COUNT = 5
QUIZ_COUNT = 10

CORRECT_PAUSE = 0.6  # brief — no need to linger on a right answer
MISS_PAUSE = 1.8  # long enough to actually read the correction before it clears


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.strip().lower()
    s = s.replace(" ", "").replace("-", "")
    return s


def init(conn) -> None:
    db.init_kana_batches(conn, BATCH_ORDER)


def select_batch(conn, batch_id: str) -> None:
    """Manually pin a batch to work on, unlocking it immediately if it's
    still locked — lets the learner jump forward past where the curriculum
    would naturally be, or back to any earlier batch, on demand."""
    if batch_id not in BATCHES_BY_ID:
        raise ValueError(f"Unknown batch id: {batch_id}")
    row = db.get_kana_batch(conn, batch_id)
    if row["status"] == "locked":
        db.set_kana_batch_status(conn, batch_id, "unlocked")


def summary(conn) -> dict:
    rows = db.all_kana_batches(conn)
    mastered = sum(1 for r in rows if r["status"] == "mastered")
    return {
        "mastered": mastered,
        "total": len(rows),
        "active_batch_id": db.get_active_kana_batch_id(conn),
    }


def _unique_targets(batch: dict) -> list[dict]:
    seen: dict[str, dict] = {}
    for item in batch["quiz_bank"]:
        if item["kana"] not in seen:
            seen[item["kana"]] = item
    return list(seen.values())


def _cycle_take(items: list, start: int, count: int) -> list:
    if not items or count <= 0:
        return []
    n = len(items)
    return [items[(start + i) % n] for i in range(count)]


def _lifetime_and_recent(conn, batch_id: str, kana: str, phase: str = "recognition", recent_n: int = 8):
    rows = db.kana_attempts_for(conn, batch_id, kana, phase)  # newest first
    return rows, rows[:recent_n]


def is_weak(conn, batch_id: str, kana: str) -> bool:
    rows, recent8 = _lifetime_and_recent(conn, batch_id, kana)
    if len(rows) < 4:
        return True
    if recent8 and sum(r["correct"] for r in recent8) / len(recent8) < 0.80:
        return True
    if rows and not rows[0]["correct"]:
        return True
    return False


def priority_score(conn, batch_id: str, kana: str) -> float:
    rows, recent8 = _lifetime_and_recent(conn, batch_id, kana)
    most_recent_wrong = 1 if rows and not rows[0]["correct"] else 0
    errors_in_4 = sum(1 for r in rows[:4] if not r["correct"])
    errors_in_8 = sum(1 for r in recent8 if not r["correct"])
    if rows:
        days_since = (date.today() - date.fromisoformat(rows[0]["local_date"])).days
    else:
        days_since = 14
    return 100 * most_recent_wrong + 20 * errors_in_4 + 5 * errors_in_8 + min(days_since, 14)


def _prior_targets(conn) -> list[dict]:
    """Unique targets from every MASTERED batch (i.e. excluding the active batch)."""
    targets = []
    for row in db.all_kana_batches(conn):
        if row["status"] != "mastered":
            continue
        batch = BATCHES_BY_ID[row["batch_id"]]
        for occ_idx, t in enumerate(_unique_targets(batch)):
            targets.append(
                {
                    "batch_id": row["batch_id"],
                    "kana": t["kana"],
                    "romaji": t["romaji"],
                    "order_index": row["order_index"],
                    "occ_idx": occ_idx,
                }
            )
    return targets


def _sorted_weak_targets(conn) -> list[dict]:
    prior = _prior_targets(conn)
    weak = [t for t in prior if is_weak(conn, t["batch_id"], t["kana"])]
    for t in weak:
        t["priority"] = priority_score(conn, t["batch_id"], t["kana"])
    weak.sort(key=lambda t: (-t["priority"], t["order_index"], t["occ_idx"]))
    return weak


def _sorted_nonweak_targets(conn) -> list[dict]:
    prior = _prior_targets(conn)
    nonweak = [t for t in prior if not is_weak(conn, t["batch_id"], t["kana"])]
    for t in nonweak:
        rows, _ = _lifetime_and_recent(conn, t["batch_id"], t["kana"])
        t["days_since"] = (date.today() - date.fromisoformat(rows[0]["local_date"])).days if rows else 14
    nonweak.sort(key=lambda t: (-t["days_since"], t["order_index"], t["occ_idx"]))
    return nonweak


def build_review_prompts(conn) -> list[dict]:
    pool = _sorted_weak_targets(conn) + _sorted_nonweak_targets(conn)
    if not pool:
        return []
    start = db.get_kana_cursor(conn, "review")
    taken = _cycle_take(pool, start, REVIEW_COUNT)
    db.bump_kana_cursor(conn, "review", REVIEW_COUNT)
    random.shuffle(taken)  # selection order (weak-first) already locked in; randomize display only
    return taken


def build_drill_prompts(conn, active_batch_id: str, isolated: bool = False) -> list[dict]:
    """isolated drops the weak/spaced cross-batch slots entirely, drilling
    only the given batch's own characters — used for a manually selected
    batch (see select_batch) so practice stays confined to what was picked
    instead of pulling in material from other batches."""
    weak_sorted = [] if isolated else _sorted_weak_targets(conn)
    nonweak_sorted = [] if isolated else _sorted_nonweak_targets(conn)

    weak_n = DRILL_WEAK if weak_sorted else 0
    spaced_n = DRILL_SPACED if nonweak_sorted else 0
    current_n = DRILL_COUNT - weak_n - spaced_n

    prompts = []
    current_bank = BATCHES_BY_ID[active_batch_id]["quiz_bank"]
    batch_row = db.get_kana_batch(conn, active_batch_id)
    taken = _cycle_take(current_bank, batch_row["drill_cursor"], current_n)
    prompts += [{"batch_id": active_batch_id, "kana": t["kana"], "romaji": t["romaji"]} for t in taken]
    db.bump_kana_cursor_column(conn, active_batch_id, "drill_cursor", current_n)

    if weak_n:
        taken = _cycle_take(weak_sorted, db.get_kana_cursor(conn, "drill_weak"), weak_n)
        prompts += taken
        db.bump_kana_cursor(conn, "drill_weak", weak_n)
    if spaced_n:
        taken = _cycle_take(nonweak_sorted, db.get_kana_cursor(conn, "drill_spaced"), spaced_n)
        prompts += taken
        db.bump_kana_cursor(conn, "drill_spaced", spaced_n)

    # The 12/6/2 split above determines WHICH characters get drilled — that
    # stays exact. Shuffling here only changes the order they're presented
    # in, so the current/weak/spaced blocks interleave instead of always
    # appearing in three predictable chunks.
    random.shuffle(prompts)
    return prompts


def build_reading_prompts(conn, active_batch_id: str) -> list[dict]:
    words = BATCHES_BY_ID[active_batch_id]["example_words"]
    batch_row = db.get_kana_batch(conn, active_batch_id)
    taken = _cycle_take(words, batch_row["reading_cursor"], READING_COUNT)
    db.bump_kana_cursor_column(conn, active_batch_id, "reading_cursor", READING_COUNT)
    random.shuffle(taken)
    return [
        {"batch_id": active_batch_id, "kana": w["kana"], "romaji": w["romaji"], "meaning": w.get("meaning")}
        for w in taken
    ]


def build_quiz_prompts(conn, active_batch_id: str) -> list[dict]:
    bank = BATCHES_BY_ID[active_batch_id]["quiz_bank"]
    batch_row = db.get_kana_batch(conn, active_batch_id)
    taken = _cycle_take(bank, batch_row["quiz_cursor"], QUIZ_COUNT)
    db.bump_kana_cursor_column(conn, active_batch_id, "quiz_cursor", QUIZ_COUNT)
    random.shuffle(taken)
    return [{"batch_id": active_batch_id, "kana": t["kana"], "romaji": t["romaji"]} for t in taken]


def teach_new_material(conn, console: Console, active_batch: dict) -> None:
    batch_row = db.get_kana_batch(conn, active_batch["id"])
    if batch_row["status"] == "unlocked":
        db.set_kana_batch_status(conn, active_batch["id"], "introduced")
        chars = active_batch["characters"]
        if chars:
            for c in chars:
                console.print(f"  {c['kana']}  →  {c['romaji']}")
            console.print("  [dim](and again, reversed)[/dim]")
            for c in reversed(chars):
                console.print(f"  {c['kana']}  →  {c['romaji']}")
        else:
            for w in active_batch["example_words"]:
                console.print(f"  {w['kana']}  →  {w['romaji']} ({w['meaning']})")
    elif batch_row["status"] == "mastered":
        console.print("  [dim]Already mastered — this is a bonus practice round, picked manually.[/dim]")
    else:
        targets = _unique_targets(active_batch)
        weak_now = []
        for t in targets:
            recent4 = db.kana_attempts_for(conn, active_batch["id"], t["kana"])[:4]
            if sum(r["correct"] for r in recent4) < 3:
                weak_now.append(t)
        if weak_now:
            console.print("  [dim]Quick refresher on characters you're still shaky on:[/dim]")
            for t in weak_now:
                console.print(f"  {t['kana']}  →  {t['romaji']}")
        else:
            console.print("  [dim]Nothing new to teach — you know these solidly. Moving to drill.[/dim]")

        sessions = db.distinct_kana_attempt_sessions(conn, active_batch["id"])
        qualifying = [s for s in sessions if db.attempts_in_session(conn, active_batch["id"], s) >= 10]
        console.print(
            f"  [dim]Mastery progress: {len(qualifying)}/2 qualifying sessions logged "
            f"— {'one more good round should unlock the next batch' if len(qualifying) == 1 else 'complete this round to check'}.[/dim]"
        )


def _run_scored_round(
    conn, console: Console, prompts: list[dict], phase: str, session_id: int,
    reveal_answer: bool = True, header: str = "",
):
    """show_correctness is always on — the learner sees ✓/✗ after every
    answer so they never rehearse a wrong reading unaware. reveal_answer
    additionally shows the correct romaji (and meaning, for kanji) on a
    miss; quiz rounds turn that off to keep some test rigor while still
    confirming right vs. wrong immediately.

    Each prompt still gets a clean, single-card screen (no backlog of past
    answers to read off of) — but there's a brief pause after feedback,
    longer on a miss, before the screen clears to the next card, so the
    feedback is actually readable instead of vanishing instantly."""
    correct_n = 0
    results = []
    for i, p in enumerate(prompts):
        console.clear()
        if header:
            console.print(f"{header} [dim]({i + 1}/{len(prompts)})[/dim]\n")
        raw = console.input(f"  {p['kana']}  ")
        ok = normalize(raw) == normalize(p["romaji"])
        db.record_kana_attempt(conn, p["batch_id"], p["kana"], phase, ok, session_id=session_id)
        correct_n += int(ok)
        if ok:
            console.print("  [green]correct[/green]")
            time.sleep(CORRECT_PAUSE)
        elif reveal_answer:
            meaning = _KANA_MEANING.get(p["kana"])
            suffix = f" — {meaning}" if meaning else ""
            console.print(f"  [red]✗ it's '{p['romaji']}'{suffix}[/red]")
            time.sleep(MISS_PAUSE)
        else:
            console.print("  [red]✗ not quite[/red]")
            time.sleep(MISS_PAUSE)
        results.append((p, ok))
    return correct_n, results


def _run_reading_round(conn, console: Console, prompts: list[dict], session_id: int, header: str = "") -> int:
    correct_n = 0
    for i, p in enumerate(prompts):
        console.clear()
        if header:
            console.print(f"{header} [dim]({i + 1}/{len(prompts)})[/dim]\n")
        raw = console.input(f"  {p['kana']}  ")
        ok = normalize(raw) == normalize(p["romaji"])
        db.record_kana_attempt(conn, p["batch_id"], p["kana"], "reading", ok, session_id=session_id)
        correct_n += int(ok)
        tag = "[green]✓[/green]" if ok else "[red]✗[/red]"
        meaning = f" — {p['meaning']}" if p.get("meaning") else ""
        console.print(f"  {tag} {p['romaji']}{meaning}")
        time.sleep(CORRECT_PAUSE if ok else MISS_PAUSE)
    return correct_n


def evaluate_mastery(conn, active_batch_id: str, reading_score: int) -> bool:
    row = db.get_kana_batch(conn, active_batch_id)
    if row["status"] != "introduced":
        return False
    targets = _unique_targets(BATCHES_BY_ID[active_batch_id])

    sessions = db.distinct_kana_attempt_sessions(conn, active_batch_id)
    if len(sessions) < 2:
        return False
    qualifying_sessions = [s for s in sessions if db.attempts_in_session(conn, active_batch_id, s) >= 10]
    if len(qualifying_sessions) < 2:
        return False

    for t in targets:
        if len(db.kana_attempts_for(conn, active_batch_id, t["kana"])) < 4:
            return False

    recent20 = db.kana_attempts_for_batch(conn, active_batch_id, "recognition", limit=20)
    if not recent20 or sum(r["correct"] for r in recent20) / len(recent20) < 0.90:
        return False

    for t in targets:
        recent4 = db.kana_attempts_for(conn, active_batch_id, t["kana"])[:4]
        if sum(r["correct"] for r in recent4) < 3:
            return False

    if reading_score < 4:
        return False

    return True


def mastery_checklist(conn, batch_id: str) -> list[dict]:
    """Same conditions as evaluate_mastery, but itemized with human-readable
    detail so `nihongo curriculum` can show exactly what's blocking mastery."""
    targets = _unique_targets(BATCHES_BY_ID[batch_id])

    sessions = db.distinct_kana_attempt_sessions(conn, batch_id)
    qualifying_sessions = [s for s in sessions if db.attempts_in_session(conn, batch_id, s) >= 10]

    under_reps = [t["kana"] for t in targets if len(db.kana_attempts_for(conn, batch_id, t["kana"])) < 4]

    recent20 = db.kana_attempts_for_batch(conn, batch_id, "recognition", limit=20)
    accuracy = (sum(r["correct"] for r in recent20) / len(recent20)) if recent20 else 0.0

    inconsistent = []
    for t in targets:
        recent4 = db.kana_attempts_for(conn, batch_id, t["kana"])[:4]
        if sum(r["correct"] for r in recent4) < 3:
            inconsistent.append(t["kana"])

    return [
        {
            "label": "Practiced across 2+ sessions",
            "met": len(sessions) >= 2,
            "detail": f"{len(sessions)} session(s) so far",
        },
        {
            "label": "10+ attempts in 2+ of those sessions",
            "met": len(qualifying_sessions) >= 2,
            "detail": f"{len(qualifying_sessions)} qualifying session(s)",
        },
        {
            "label": "Every character has 4+ attempts",
            "met": not under_reps,
            "detail": "all covered" if not under_reps else f"still needs reps: {', '.join(under_reps)}",
        },
        {
            "label": "90%+ accuracy (last 20 attempts)",
            "met": bool(recent20) and accuracy >= 0.90,
            "detail": f"{accuracy * 100:.0f}% over {len(recent20)} attempt(s)",
        },
        {
            "label": "Every character: 3 of last 4 correct",
            "met": not inconsistent,
            "detail": "all solid" if not inconsistent else f"still shaky: {', '.join(inconsistent)}",
        },
    ]


def course_overview(conn) -> list[dict]:
    """Per-batch status for `nihongo curriculum` — the full day-by-day map."""
    overview = []
    for row in db.all_kana_batches(conn):
        batch_id = row["batch_id"]
        targets = _unique_targets(BATCHES_BY_ID[batch_id])
        if row["status"] == "locked":
            overview.append({"id": batch_id, "status": "locked", "accuracy": None, "weak": None, "total": len(targets)})
            continue
        recent20 = db.kana_attempts_for_batch(conn, batch_id, "recognition", limit=20)
        accuracy = (sum(r["correct"] for r in recent20) / len(recent20)) if recent20 else None
        weak = sum(1 for t in targets if is_weak(conn, batch_id, t["kana"]))
        overview.append(
            {"id": batch_id, "status": row["status"], "accuracy": accuracy, "weak": weak, "total": len(targets)}
        )
    return overview


_KANA_ROMAJI: dict[str, str] = {}
for _batch in BATCHES:
    for _t in _batch["quiz_bank"]:
        _KANA_ROMAJI.setdefault(_t["kana"], _t["romaji"])

# Meanings only exist for single-character entries in example_words — in
# practice that's kanji (水 → water), since kana batches' example words are
# multi-character. Feeds the meaning shown alongside a missed kanji answer.
_KANA_MEANING: dict[str, str] = {}
for _batch in BATCHES:
    for _w in _batch.get("example_words", []):
        if _w.get("meaning"):
            _KANA_MEANING.setdefault(_w["kana"], _w["meaning"])


def character_progress(conn) -> list[dict]:
    """Lifetime stats for every character ever drilled, worst-accuracy first
    — the per-character detail `nihongo progress` shows beyond curriculum's
    per-batch weak-count summary."""
    rows = db.kana_char_stats(conn)
    out = []
    for r in rows:
        accuracy = r["correct"] / r["attempts"] if r["attempts"] else 0.0
        out.append(
            {
                "batch_id": r["batch_id"],
                "kana": r["kana"],
                "romaji": _KANA_ROMAJI.get(r["kana"], "?"),
                "attempts": r["attempts"],
                "correct": r["correct"],
                "accuracy": accuracy,
                "last_date": r["last_date"],
                "weak": is_weak(conn, r["batch_id"], r["kana"]),
            }
        )
    out.sort(key=lambda c: (c["accuracy"], -c["attempts"]))
    return out


def _unlock_next(conn, active_batch_id: str) -> str | None:
    idx = BATCH_ORDER.index(active_batch_id)
    if idx + 1 < len(BATCH_ORDER):
        next_id = BATCH_ORDER[idx + 1]
        db.set_kana_batch_status(conn, next_id, "unlocked")
        return next_id
    return None


def _run_maintenance(conn, console: Console, session_id: int) -> None:
    console.clear()
    console.print("[bold cyan]Kana — maintenance review[/bold cyan] [dim](every batch is mastered!)[/dim]\n")

    pool = _sorted_weak_targets(conn) + _sorted_nonweak_targets(conn)
    taken = _cycle_take(pool, db.get_kana_cursor(conn, "maintenance_review"), 20)
    db.bump_kana_cursor(conn, "maintenance_review", 20)
    random.shuffle(taken)
    _run_scored_round(
        conn, console, taken, "recognition", session_id,
        header="[bold cyan]Maintenance[/bold cyan] — [bold]Review[/bold]",
    )

    all_words = []
    for bid in [r["batch_id"] for r in db.all_kana_batches(conn) if r["status"] == "mastered"]:
        for w in BATCHES_BY_ID[bid]["example_words"]:
            all_words.append({"batch_id": bid, "kana": w["kana"], "romaji": w["romaji"], "meaning": w.get("meaning")})
    taken_r = _cycle_take(all_words, db.get_kana_cursor(conn, "maintenance_reading"), 10)
    db.bump_kana_cursor(conn, "maintenance_reading", 10)
    random.shuffle(taken_r)
    _run_reading_round(
        conn, console, taken_r, session_id,
        header="[bold cyan]Maintenance[/bold cyan] — [bold]Reading[/bold]",
    )


def _run_one_round(conn, console: Console, active_id: str, session_id: int, isolated: bool = False) -> dict:
    """One full review→teach→drill→read→quiz round for the active batch.
    Returns a result dict; raises EOFError if the learner bails mid-round.

    isolated confines the round to just this batch's own material — used
    when the learner manually selected this batch (see select_batch), so
    practice stays independent instead of pulling in review/drill content
    from other batches: it drops the opening cross-batch review round, and
    drill draws all 20 slots from this batch instead of reserving some for
    other batches' weak/spaced characters."""
    active = BATCHES_BY_ID[active_id]
    header = f"[bold cyan]{active_id}[/bold cyan]"

    console.print(f"{header}\n")

    review_prompts = [] if isolated else build_review_prompts(conn)
    if review_prompts:
        _run_scored_round(
            conn, console, review_prompts, "recognition", session_id,
            header=f"{header} — [bold]Review[/bold]",
        )

    console.clear()
    console.print(f"{header} — [bold]New material[/bold]\n")
    teach_new_material(conn, console, active)
    console.input("\n[dim]Press enter when ready to drill...[/dim]")

    drill_prompts = build_drill_prompts(conn, active_id, isolated=isolated)
    drill_correct, _ = _run_scored_round(
        conn, console, drill_prompts, "recognition", session_id,
        header=f"{header} — [bold]Drill[/bold]",
    )
    console.clear()
    console.print(f"{header} — [bold]Drill results[/bold]\n\n  {drill_correct}/{len(drill_prompts)} correct")
    console.input("\n[dim]Press enter to continue to reading...[/dim]")

    reading_prompts = build_reading_prompts(conn, active_id)
    reading_correct = _run_reading_round(
        conn, console, reading_prompts, session_id, header=f"{header} — [bold]Reading[/bold]"
    )
    console.input("\n[dim]Press enter to continue to the quiz...[/dim]")

    quiz_prompts = build_quiz_prompts(conn, active_id)
    quiz_correct, quiz_results = _run_scored_round(
        conn, console, quiz_prompts, "recognition", session_id, reveal_answer=False,
        header=f"{header} — [bold]Quiz[/bold] [dim](✓/✗ shown, correct answers revealed at the end)[/dim]",
    )

    console.clear()
    console.print(f"{header} — [bold]Quiz results[/bold]\n\n  Score: {quiz_correct}/{len(quiz_prompts)}")
    for p, ok in quiz_results:
        if not ok:
            console.print(f"    [red]{p['kana']} → {p['romaji']}[/red]")

    mastered_batch_id = None
    course_complete = False
    if evaluate_mastery(conn, active_id, reading_correct):
        db.set_kana_batch_status(conn, active_id, "mastered")
        mastered_batch_id = active_id
        console.print(f"\n[bold green]✅ {active_id} mastered![/bold green]")
        next_id = _unlock_next(conn, active_id)
        if next_id is None:
            db.complete_current_unit(conn)
            course_complete = True
            console.print(
                "[bold green]🎉 Full kana curriculum complete! "
                "Moving on to grammar & conversation.[/bold green]"
            )

    return {
        "quiz_correct": quiz_correct,
        "quiz_total": len(quiz_prompts),
        "mastered_batch_id": mastered_batch_id,
        "course_complete": course_complete,
    }


def run_session(conn, console: Console, batch_id: str | None = None) -> None:
    # The whole sitting — however many rounds you do — runs in the
    # terminal's alternate screen buffer, like vim/htop, so none of it
    # lands in your normal scrollback. Only the final summary prints after
    # the `with` block exits, so that's the one thing left visible.
    #
    # batch_id pins every round in this sitting to one manually-chosen batch
    # instead of the auto-selected earliest-incomplete one (see select_batch)
    # — the learner's deliberate choice to work forward or backward in the
    # curriculum takes priority over the normal progression for as long as
    # this session runs; the next plain `nihongo kana` goes back to auto mode.
    init(conn)
    if batch_id is not None:
        select_batch(conn, batch_id)
    rounds = 0
    mastered_this_sitting = []
    course_complete = False
    ended_early = False

    try:
        with console.screen(hide_cursor=False):
            while True:
                active_id = batch_id if batch_id is not None else db.get_active_kana_batch_id(conn)
                session_id = db.start_session(conn, "kana", "kana_mastery")
                try:
                    if active_id is None:
                        _run_maintenance(conn, console, session_id)
                        outcome = {}
                    else:
                        outcome = _run_one_round(
                            conn, console, active_id, session_id, isolated=batch_id is not None
                        )
                except EOFError:
                    db.end_session(conn, session_id)
                    ended_early = True
                    break
                db.end_session(conn, session_id)
                rounds += 1

                if outcome.get("mastered_batch_id"):
                    mastered_this_sitting.append(outcome["mastered_batch_id"])
                if outcome.get("course_complete"):
                    course_complete = True
                    break

                console.clear()
                try:
                    cont = console.input(
                        f"\n[dim]Round {rounds} done. Keep going? (y/n)[/dim] "
                    )
                except EOFError:
                    break
                if not cont.strip().lower().startswith("y"):
                    break
    except EOFError:
        ended_early = True

    if ended_early:
        console.print(f"[dim]Session ended early after {rounds} round(s) — progress so far is saved.[/dim]")
        return

    console.print(f"[bold]{rounds} round(s) completed.[/bold]")
    if mastered_this_sitting:
        console.print(f"[bold green]✅ Mastered: {', '.join(mastered_this_sitting)}[/bold green]")
    if course_complete:
        console.print(
            "[bold green]🎉 Full kana curriculum complete! "
            "Moving on to grammar & conversation.[/bold green]"
        )
    console.print("[dim]See you next time.[/dim]")
