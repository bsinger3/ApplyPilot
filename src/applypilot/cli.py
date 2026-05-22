"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
persona_app = typer.Typer(help="Manage job-search personas.")
app.add_typer(persona_app, name="persona")
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


def _safe_console_text(value: object) -> str:
    """Return text that Windows PowerShell's legacy console can print."""
    text = "" if value is None else str(value)
    return text.encode("cp1252", errors="replace").decode("cp1252")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@persona_app.command("list")
def persona_list() -> None:
    """List configured personas."""
    _bootstrap()

    from applypilot.database import get_connection, ensure_default_persona

    conn = get_connection()
    ensure_default_persona(conn)
    rows = conn.execute("SELECT slug, name, profile_path, resume_path FROM personas ORDER BY slug").fetchall()

    table = Table(title="ApplyPilot Personas", show_header=True, header_style="bold cyan")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Profile")
    table.add_column("Resume")
    for row in rows:
        table.add_row(row["slug"], row["name"], row["profile_path"] or "", row["resume_path"] or "")
    console.print(table)


@persona_app.command("create")
def persona_create(
    slug: str = typer.Argument(..., help="Persona slug, for example software-pm."),
    name: Optional[str] = typer.Option(None, "--name", help="Display name."),
) -> None:
    """Create a persona record with conventional file paths."""
    _bootstrap()

    from applypilot.database import create_persona, get_connection
    from applypilot.config import ensure_persona_dirs, resolve_persona_paths

    conn = get_connection()
    row = create_persona(slug, name=name, conn=conn)
    paths = ensure_persona_dirs(resolve_persona_paths(row))
    console.print(f"[green]Persona ready:[/green] {row['slug']}")
    console.print(f"  Profile: {paths.profile_path}")
    console.print(f"  Resume:  {paths.resume_path}")
    console.print(f"  Search:  {paths.search_config_path}")


@persona_app.command("show")
def persona_show(
    slug: str = typer.Argument(..., help="Persona slug to show."),
) -> None:
    """Show persona paths."""
    _bootstrap()

    from applypilot.database import get_connection, get_persona_by_slug
    from applypilot.config import resolve_persona_paths

    row = get_persona_by_slug(slug, conn=get_connection())
    paths = resolve_persona_paths(row)
    console.print(f"\n[bold]{row['slug']}[/bold] ({row['name']})")
    console.print(f"  Profile:       {paths.profile_path}")
    console.print(f"  Resume:        {paths.resume_path}")
    console.print(f"  Resume PDF:    {paths.resume_pdf_path}")
    console.print(f"  Searches:      {paths.search_config_path}")
    console.print(f"  Tailored dir:  {paths.tailored_dir}")
    console.print(f"  Cover letters: {paths.cover_letter_dir}\n")


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    persona: str = typer.Option(..., "--persona", help="Persona slug to use for search/scoring/tailoring."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline
    from applypilot.database import get_connection, get_persona_by_slug

    try:
        get_persona_by_slug(persona, conn=get_connection())
    except FileNotFoundError:
        console.print(f"[red]Persona not found:[/red] {persona}")
        raise typer.Exit(code=1)

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        persona=persona,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    backend: str = typer.Option(
        "claude",
        "--backend",
        help="Auto-apply backend: claude (current) or stagehand (experimental Browserbase/Stagehand).",
    ),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    persona: str = typer.Option(..., "--persona", help="Persona slug to use for auto-apply."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import APP_DIR, check_tier, resolve_persona_paths
    from applypilot.database import get_connection, get_persona_by_slug

    conn = get_connection()
    try:
        persona_row = get_persona_by_slug(persona, conn=conn)
    except FileNotFoundError:
        console.print(f"[red]Persona not found:[/red] {persona}")
        raise typer.Exit(code=1)
    persona_paths = resolve_persona_paths(persona_row)

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied", persona=persona)
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason, persona=persona)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset(persona=persona)
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    if backend not in {"claude", "stagehand"}:
        console.print("[red]Invalid --backend.[/red] Choose: claude, stagehand")
        raise typer.Exit(code=1)

    # Check 1: backend-specific requirements
    if backend == "claude":
        check_tier(3, "auto-apply")
    else:
        from applypilot.config import load_env
        import os
        import shutil

        load_env()
        missing = []
        if not os.environ.get("BROWSERBASE_API_KEY"):
            missing.append("BROWSERBASE_API_KEY in ~/.applypilot/.env")
        if not shutil.which("npm"):
            missing.append("Node.js/npm")
        if missing:
            console.print("[red]Stagehand backend is missing requirements:[/red]")
            for item in missing:
                console.print(f"  - {item}")
            raise typer.Exit(code=1)

    # Check 2: Profile exists
    if not persona_paths.profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            f"Create a profile for persona [bold]{persona}[/bold]: {persona_paths.profile_path}"
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        ready = conn.execute(
            """
            SELECT COUNT(*) FROM job_persona jp
            JOIN jobs j ON j.id = jp.job_id
            WHERE jp.persona_id = ?
              AND jp.tailored_resume_path IS NOT NULL
              AND jp.applied_at IS NULL
              AND COALESCE(j.application_url_canonical, j.application_url) IS NOT NULL
            """,
            (persona_row["id"],),
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                f"Run [bold]applypilot run score tailor --persona {persona}[/bold] first."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model, persona=persona)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = APP_DIR / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Persona:  {persona}")
    console.print(f"  Backend:  {backend}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    if backend == "stagehand":
        if workers != 1:
            console.print("[yellow]Stagehand experiment currently runs one worker; ignoring --workers.[/yellow]")
        if continuous:
            console.print("[yellow]Stagehand experiment does not support --continuous yet.[/yellow]")
        from applypilot.apply.stagehand_backend import main as stagehand_main

        stagehand_main(
            limit=1 if effective_limit == 0 else effective_limit,
            target_url=url,
            min_score=min_score,
            dry_run=True if dry_run else False,
            persona=persona,
        )
    else:
        apply_main(
            limit=effective_limit,
            target_url=url,
            min_score=min_score,
            headless=headless,
            model=model,
            dry_run=dry_run,
            continuous=continuous,
            workers=workers,
            persona=persona,
        )


@app.command()
def status(
    persona: str = typer.Option(..., "--persona", help="Persona slug to report."),
) -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_connection, get_persona_by_slug, get_stats

    conn = get_connection()
    persona_row = get_persona_by_slug(persona, conn=conn)
    stats = get_stats(conn=conn, persona_id=persona_row["id"])

    console.print(f"\n[bold]ApplyPilot Pipeline Status[/bold] [dim]persona={persona}[/dim]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(_safe_console_text(site or "Unknown"), str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard(
    persona: str = typer.Option(..., "--persona", help="Persona slug to display."),
) -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard(persona=persona)


@app.command()
def doctor(
    persona: Optional[str] = typer.Option(None, "--persona", help="Check persona-specific profile, resume, and search files."),
) -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path, resolve_persona_paths,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    persona_paths = None
    if persona:
        from applypilot.database import get_connection, get_persona_by_slug

        try:
            persona_row = get_persona_by_slug(persona, conn=get_connection())
        except FileNotFoundError:
            console.print(f"[red]Persona not found:[/red] {persona}")
            raise typer.Exit(code=1)
        persona_paths = resolve_persona_paths(persona_row)

    # --- Tier 1 checks ---
    # Profile
    profile_path = persona_paths.profile_path if persona_paths else PROFILE_PATH
    if profile_path.exists():
        results.append(("profile.json", ok_mark, str(profile_path)))
    else:
        if persona:
            results.append(("profile.json", fail_mark, f"Create {profile_path}"))
        else:
            results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    resume_path = persona_paths.resume_path if persona_paths else RESUME_PATH
    resume_pdf_path = persona_paths.resume_pdf_path if persona_paths else RESUME_PDF_PATH
    if resume_path.exists():
        results.append(("resume.txt", ok_mark, str(resume_path)))
    elif resume_pdf_path.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found - plain-text needed for AI stages"))
    else:
        if persona:
            results.append(("resume.txt", fail_mark, f"Create {resume_path}"))
        else:
            results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    search_config_path = persona_paths.search_config_path if persona_paths else SEARCH_CONFIG_PATH
    if search_config_path.exists():
        results.append(("searches.yaml", ok_mark, str(search_config_path)))
    else:
        if persona:
            results.append(("searches.yaml", warn_mark, f"Will use example config - create {search_config_path}"))
        else:
            results.append(("searches.yaml", warn_mark, "Will use example config - run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    title = "ApplyPilot Doctor" if not persona else f"ApplyPilot Doctor [dim]persona={persona}[/dim]"
    console.print(f"[bold]{title}[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} - {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  -> Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  -> Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  -> Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
