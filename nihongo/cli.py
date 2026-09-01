import os
from datetime import date, datetime, timedelta
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from . import curriculum, db, kana_engine

app = typer.Typer(add_completion=False, no_args_is_help=False)
console = Console()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    load_dotenv(_PROJECT_ROOT / ".env")
    load_dotenv()  # allow a cwd-local .env to override


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _compute_streak(conn) -> int:
    rows = conn.execute(
        "SELECT DISTINCT date(started_at) as d FROM sessions ORDER BY d DESC"
    ).fetchall()
    if not rows:
        return 0
    dates = {datetime.fromisoformat(r["d"]).date() for r in rows}
    streak = 0
    day = date.today()
    while day in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        status()


@app.command()
def status():
    """Show current unit, streak, and items due — the default view."""
    _load_env()
    conn = db.connect()
    kana_engine.init(conn)

    progress = db.get_progress(conn)
    unit = curriculum.get_unit(progress["unit_id"])
    streak = _compute_streak(conn)

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_row("Current unit", f"[bold]{unit.title}[/bold]")
    table.add_row("Status", progress["status"])
    table.add_row("Streak", f"{streak} day(s)")

    if unit.engine == "kana_offline":
        kstats = kana_engine.summary(conn)
        table.add_row("Kana batches mastered", f"{kstats['mastered']}/{kstats['total']}")
        table.add_row("Active batch", kstats["active_batch_id"] or "(maintenance review)")
    else:
        stats = db.srs_stats(conn)
        table.add_row("Items due for review", str(stats["due"]))
        table.add_row("Total SRS items", str(stats["total"]))

    console.print(table)
    if unit.engine == "kana_offline":
        console.print(
            "\n[dim]Run [bold]nihongo kana[/bold] to continue, "
            "[bold]nihongo curriculum[/bold] for the full course map, or "
            "[bold]nihongo progress[/bold] for a detailed report.[/dim]"
        )
    else:
        console.print(
            "\n[dim]Run [bold]nihongo lesson[/bold] to continue, "
            "[bold]nihongo review[/bold] for vocab/grammar review, or "
            "[bold]nihongo progress[/bold] for a detailed report.[/dim]"
        )


@app.command()
def lesson():
    """Start or resume a live GPT lesson for the current unit (post-kana units only)."""
    _load_env()
    conn = db.connect()
    unit = curriculum.get_unit(db.get_progress(conn)["unit_id"])

    if unit.engine == "kana_offline":
        console.print(
            "[dim]The current unit (kana) runs offline — use "
            "[bold]nihongo kana[/bold] instead.[/dim]"
        )
        return

    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[bold red]OPENAI_API_KEY is not set.[/bold red] Copy .env.example to .env and fill it in.")
        raise typer.Exit(1)

    from . import tutor

    tutor.run_lesson(conn, console)


_STATUS_LABELS = {
    "locked": "[dim]locked[/dim]",
    "unlocked": "[cyan]next up[/cyan]",
    "introduced": "[yellow]in progress[/yellow]",
    "mastered": "[green]mastered[/green]",
}


def _batch_overview_table(conn, title: str) -> Table:
    table = Table(title=title)
    table.add_column("#", justify="right")
    table.add_column("Batch")
    table.add_column("Status")
    table.add_column("Accuracy", justify="right")
    table.add_column("Weak", justify="right")
    for i, row in enumerate(kana_engine.course_overview(conn), start=1):
        label = row["id"].replace("_", " ").title()
        acc = f"{row['accuracy'] * 100:.0f}%" if row["accuracy"] is not None else "[dim]—[/dim]"
        weak = str(row["weak"]) if row["weak"] is not None else "[dim]—[/dim]"
        table.add_row(str(i), label, _STATUS_LABELS[row["status"]], acc, weak)
    return table


def _pick_batch(conn) -> str | None:
    overview = kana_engine.course_overview(conn)
    console.print(_batch_overview_table(conn, "Pick a batch to work on"))
    console.print(
        "\n[dim]Locked batches unlock immediately if you pick them — "
        "jump forward or back as you like.[/dim]"
    )
    raw = console.input("Enter a batch number (or press enter to cancel): ").strip()
    if not raw:
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= len(overview)):
        console.print("[red]Not a valid batch number.[/red]")
        return None
    return overview[int(raw) - 1]["id"]


@app.command()
def kana(select: bool = False):
    """Run one offline kana session: review, new material, drill, reading, quiz.

    Pass --select to manually pick which batch to work on instead of
    continuing automatically — locked batches unlock immediately, so you can
    jump forward or backward through the curriculum on demand. A manually
    selected batch is isolated from the rest of the curriculum: no opening
    review of already-mastered material, and drill draws only from that
    batch's own characters instead of mixing in others'.
    """
    _load_env()
    conn = db.connect()
    kana_engine.init(conn)

    batch_id = None
    if select:
        batch_id = _pick_batch(conn)
        if batch_id is None:
            console.print("[dim]Cancelled.[/dim]")
            return

    kana_engine.run_session(conn, console, batch_id=batch_id)


@app.command(name="curriculum")
def curriculum_map():
    """Show the full kana course map — every batch's status/mastery, and what's blocking the current one."""
    _load_env()
    conn = db.connect()
    kana_engine.init(conn)

    console.print(_batch_overview_table(conn, "Kana curriculum"))

    active_id = db.get_active_kana_batch_id(conn)
    if active_id:
        console.print(f"\n[bold]Focus — {active_id.replace('_', ' ').title()}[/bold] (what's blocking mastery):")
        for item in kana_engine.mastery_checklist(conn, active_id):
            mark = "[green]✓[/green]" if item["met"] else "[red]✗[/red]"
            console.print(f"  {mark} {item['label']} — [dim]{item['detail']}[/dim]")
        console.print("  [dim](plus: that session's reading check needs 4/5)[/dim]")


@app.command()
def progress(sessions: int = 10, chars: int = 15, all_chars: bool = False, items: int = 20):
    """Detailed progress report: overview stats, session history, a
    per-character kana breakdown, and SRS vocab/grammar detail."""
    _load_env()
    conn = db.connect()
    kana_engine.init(conn)

    # --- Overview -----------------------------------------------------
    n_attempts, n_correct = db.total_kana_accuracy(conn)
    kstats = kana_engine.summary(conn)
    srs_summary = db.srs_stats(conn)
    streak = _compute_streak(conn)
    session_totals = db.session_count(conn)
    practice_seconds = db.total_practice_seconds(conn)

    overview = Table(title="Overview", show_header=False, box=None, padding=(0, 1))
    overview.add_row("Streak", f"{streak} day(s)")
    kinds = ", ".join(f"{v} {k}" for k, v in session_totals["by_kind"].items())
    overview.add_row("Sessions", f"{session_totals['total']} total ({kinds})" if kinds else "0")
    overview.add_row("Time practiced", _format_duration(practice_seconds))
    overview.add_row("Kana batches mastered", f"{kstats['mastered']}/{kstats['total']}")
    overview.add_row(
        "Kana lifetime accuracy",
        f"{n_correct / n_attempts * 100:.1f}% over {n_attempts} attempt(s)"
        if n_attempts
        else "[dim]no attempts yet[/dim]",
    )
    by_type = ", ".join(f"{k}: {v}" for k, v in srs_summary["by_type"].items())
    overview.add_row(
        "SRS items",
        f"{srs_summary['total']} total, {srs_summary['due']} due" + (f" ({by_type})" if by_type else ""),
    )
    console.print(overview)

    # --- Recent sessions ------------------------------------------------
    console.print("\n[bold]Recent sessions[/bold]")
    session_scores = db.session_kana_stats(conn)
    sess_table = Table()
    sess_table.add_column("Date")
    sess_table.add_column("Kind")
    sess_table.add_column("Unit")
    sess_table.add_column("Duration", justify="right")
    sess_table.add_column("Score", justify="right")
    for r in db.recent_sessions(conn, limit=sessions):
        started = datetime.fromisoformat(r["started_at"])
        if r["ended_at"]:
            ended = datetime.fromisoformat(r["ended_at"])
            duration = _format_duration((ended - started).total_seconds())
        else:
            duration = "[dim]in progress[/dim]"
        score = "[dim]—[/dim]"
        att, corr = session_scores.get(r["id"], (0, 0))
        if att:
            score = f"{corr}/{att} ({corr / att * 100:.0f}%)"
        sess_table.add_row(
            started.strftime("%Y-%m-%d %H:%M"), r["kind"], r["unit_id"] or "[dim]—[/dim]", duration, score
        )
    console.print(sess_table)

    # --- Kana character breakdown ---------------------------------------
    char_progress = kana_engine.character_progress(conn)
    if char_progress:
        shown = char_progress if all_chars else char_progress[:chars]
        note = (
            ""
            if all_chars or len(shown) >= len(char_progress)
            else f" [dim](weakest {len(shown)} of {len(char_progress)} — pass --all-chars for the full list)[/dim]"
        )
        console.print(f"\n[bold]Kana character breakdown[/bold]{note}")
        char_table = Table()
        char_table.add_column("Kana")
        char_table.add_column("Romaji")
        char_table.add_column("Batch")
        char_table.add_column("Attempts", justify="right")
        char_table.add_column("Accuracy", justify="right")
        char_table.add_column("Last practiced")
        for c in shown:
            style = "red" if c["accuracy"] < 0.8 else ("yellow" if c["accuracy"] < 0.9 else "green")
            char_table.add_row(
                c["kana"],
                c["romaji"],
                c["batch_id"].replace("_", " ").title(),
                str(c["attempts"]),
                f"[{style}]{c['accuracy'] * 100:.0f}%[/{style}]",
                c["last_date"] or "[dim]—[/dim]",
            )
        console.print(char_table)
    else:
        console.print("\n[dim]No kana attempts recorded yet.[/dim]")

    # --- SRS vocab/grammar detail ----------------------------------------
    srs_rows = db.all_srs_items(conn)
    if srs_rows:
        shown_srs = srs_rows[:items]
        note = (
            ""
            if len(shown_srs) >= len(srs_rows)
            else f" [dim](showing {items} of {len(srs_rows)}, soonest due first — pass --items to show more)[/dim]"
        )
        console.print(f"\n[bold]SRS vocab & grammar[/bold]{note}")
        srs_table = Table()
        srs_table.add_column("Type")
        srs_table.add_column("Prompt")
        srs_table.add_column("Answer")
        srs_table.add_column("Meaning")
        srs_table.add_column("Reps", justify="right")
        srs_table.add_column("Interval", justify="right")
        srs_table.add_column("Due date")
        srs_table.add_column("Status")
        today = date.today()
        for r in shown_srs:
            due = date.fromisoformat(r["due_date"])
            if r["reps"] == 0:
                status = "[cyan]new[/cyan]"
            elif due <= today:
                status = "[red]due[/red]"
            else:
                status = "[dim]scheduled[/dim]"
            srs_table.add_row(
                r["item_type"],
                r["prompt"],
                r["answer"],
                r["meaning"] or "[dim]—[/dim]",
                str(r["reps"]),
                f"{r['interval_days']:.0f}d",
                r["due_date"],
                status,
            )
        console.print(srs_table)
    else:
        console.print("\n[dim]No vocab/grammar items yet — those show up once you start `nihongo lesson`.[/dim]")


@app.command()
def review(limit: int = 15):
    """Self-graded review of due vocab/grammar items."""
    _load_env()
    conn = db.connect()

    due = [
        r
        for r in db.get_due_srs_items(conn, limit=limit * 2)
        if r["item_type"] in ("vocab", "grammar")
    ][:limit]

    if not due:
        console.print("[dim]Nothing due for review right now.[/dim]")
        return

    console.print(f"[bold cyan]Review — {len(due)} due[/bold cyan]\n")
    for row in due:
        console.input(f"  {row['prompt']}  (press enter to reveal) ")
        meaning = f" — {row['meaning']}" if row["meaning"] else ""
        console.print(f"  [yellow]{row['answer']}{meaning}[/yellow]")
        got_it = console.input("  Got it right? (y/n) ").strip().lower().startswith("y")
        db.review_srs_item(conn, row["id"], 5 if got_it else 1)
        console.print()


def main():
    app()


if __name__ == "__main__":
    main()
