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
            "\n[dim]Run [bold]nihongo kana[/bold] to continue, or "
            "[bold]nihongo curriculum[/bold] for the full course map.[/dim]"
        )
    else:
        console.print(
            "\n[dim]Run [bold]nihongo lesson[/bold] to continue, or "
            "[bold]nihongo review[/bold] for vocab/grammar review.[/dim]"
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


@app.command()
def kana():
    """Run one offline kana session: review, new material, drill, reading, quiz."""
    _load_env()
    conn = db.connect()
    kana_engine.run_session(conn, console)


@app.command(name="curriculum")
def curriculum_map():
    """Show the full kana course map — every batch's status/mastery, and what's blocking the current one."""
    _load_env()
    conn = db.connect()
    kana_engine.init(conn)

    table = Table(title="Kana curriculum")
    table.add_column("#", justify="right")
    table.add_column("Batch")
    table.add_column("Status")
    table.add_column("Accuracy", justify="right")
    table.add_column("Weak", justify="right")

    for i, row in enumerate(kana_engine.course_overview(conn), start=1):
        label = row["id"].replace("_", " ").title()
        acc = f"{row['accuracy'] * 100:.0f}%" if row["accuracy"] is not None else "[dim]—[/dim]"
        weak = str(row["weak"]) if row["weak"] is not None else "[dim]—[/dim]"
        status_labels = {
            "locked": "[dim]locked[/dim]",
            "unlocked": "[cyan]next up[/cyan]",
            "introduced": "[yellow]in progress[/yellow]",
            "mastered": "[green]mastered[/green]",
        }
        table.add_row(str(i), label, status_labels[row["status"]], acc, weak)

    console.print(table)

    active_id = db.get_active_kana_batch_id(conn)
    if active_id:
        console.print(f"\n[bold]Focus — {active_id.replace('_', ' ').title()}[/bold] (what's blocking mastery):")
        for item in kana_engine.mastery_checklist(conn, active_id):
            mark = "[green]✓[/green]" if item["met"] else "[red]✗[/red]"
            console.print(f"  {mark} {item['label']} — [dim]{item['detail']}[/dim]")
        console.print("  [dim](plus: that session's reading check needs 4/5)[/dim]")


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
