"""Experimental Stagehand/Browserbase apply backend.

This backend keeps the existing ApplyPilot queue/database behavior but replaces
the Claude Code + Playwright MCP loop with a small Stagehand runner. It is
intended for side-by-side dry-run experiments, not as the default apply path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console

from applypilot import config
from applypilot.apply.launcher import acquire_job, mark_result, release_lock

RESULT_PREFIX = "APPLYPILOT_STAGEHAND_RESULT "


def _copy_upload_assets(job: dict, profile: dict, worker_id: int) -> tuple[str, str, str]:
    """Copy resume/cover letter assets to a stable worker upload directory."""
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    full_name = profile.get("personal", {}).get("full_name", "Applicant")
    name_slug = re.sub(r"[^A-Za-z0-9]+", "_", full_name).strip("_") or "Applicant"
    dest_dir = config.APPLY_WORKER_DIR / f"stagehand-{worker_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    resume_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy2(src_pdf, resume_pdf)

    cover_letter_pdf = ""
    cover_letter_text = ""
    cover_letter_path = job.get("cover_letter_path")
    if cover_letter_path:
        cl_src = Path(cover_letter_path)
        cl_txt = cl_src.with_suffix(".txt")
        cl_pdf = cl_src.with_suffix(".pdf")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix.lower() == ".txt" and cl_src.exists():
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        if cl_pdf.exists():
            cl_dest = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy2(cl_pdf, cl_dest)
            cover_letter_pdf = str(cl_dest)

    return str(resume_pdf), cover_letter_pdf, cover_letter_text


def _build_payload(job: dict, dry_run: bool, persona: str, worker_id: int) -> dict:
    profile = config.load_profile(persona)
    resume_pdf, cover_letter_pdf, cover_letter_text = _copy_upload_assets(job, profile, worker_id)
    resume_text = ""
    txt_path = Path(job["tailored_resume_path"]).with_suffix(".txt")
    if txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    return {
        "url": job.get("application_url") or job["url"],
        "job": {
            "title": job["title"],
            "company": job.get("site", "Unknown"),
            "fitScore": job.get("fit_score"),
            "location": job.get("location"),
            "description": job.get("full_description"),
        },
        "profile": profile,
        "resumeText": resume_text,
        "resumePdfPath": resume_pdf,
        "coverLetterPdfPath": cover_letter_pdf,
        "coverLetterText": cover_letter_text,
        "dryRun": dry_run,
    }


def _parse_result(output: str) -> dict:
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    return {"status": "failed", "reason": "no_stagehand_result"}


def run_job(job: dict, worker_id: int = 0, dry_run: bool = True,
            persona: str = "default") -> tuple[str, int, dict]:
    """Run one job through the Stagehand experiment."""
    if not os.environ.get("BROWSERBASE_API_KEY"):
        raise RuntimeError("BROWSERBASE_API_KEY is required for --backend stagehand")

    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("Node.js/npm is required for --backend stagehand")

    payload = _build_payload(job, dry_run=dry_run, persona=persona, worker_id=worker_id)
    worker_dir = config.APPLY_WORKER_DIR / f"stagehand-{worker_id}"
    payload_path = worker_dir / "payload.json"
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "stagehand_apply.mjs"
    cmd = [npm, "run", "stagehand:apply", "--", str(payload_path)]

    start = time.time()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=config.DEFAULTS["apply_timeout"] * 2,
        cwd=str(script_path.parents[1]),
    )
    duration_ms = int((time.time() - start) * 1000)
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_site = re.sub(r"[^A-Za-z0-9]+", "_", job.get("site") or "unknown")[:30]
    log_path = config.LOG_DIR / f"stagehand_{ts}_w{worker_id}_{safe_site}.log"
    log_path.write_text(output, encoding="utf-8")

    result = _parse_result(output)
    if proc.returncode != 0 and result.get("status") == "failed":
        result["reason"] = result.get("reason") or f"stagehand_exit_{proc.returncode}"

    return str(result.get("status", "failed")), duration_ms, result


def _status_to_db(status: str, dry_run: bool) -> tuple[str, str | None, bool]:
    if status == "applied":
        return "applied", None, False
    if status in {"expired", "captcha", "login_issue"}:
        return "failed", status, True
    if status == "dry_run_ready" or dry_run:
        return "failed", "stagehand_dry_run_ready", False
    return "failed", status, False


def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, dry_run: bool = True,
         persona: str = "default") -> None:
    """Run the Stagehand apply experiment for a small number of jobs."""
    config.ensure_dirs()
    config.load_env()
    console = Console()
    applied = 0
    failed = 0

    for idx in range(limit):
        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=idx, persona=persona)
        if not job:
            console.print("[yellow]No matching job ready for Stagehand apply.[/yellow]")
            break

        console.print(
            f"[bold]Stagehand experiment[/bold]: {job['title']} @ {job.get('site', 'Unknown')}"
        )
        try:
            status, duration_ms, result = run_job(
                job,
                worker_id=idx,
                dry_run=dry_run,
                persona=persona,
            )
            db_status, reason, permanent = _status_to_db(status, dry_run=dry_run)
            if dry_run:
                release_lock(job)
            else:
                mark_result(
                    job,
                    db_status,
                    reason or result.get("reason"),
                    permanent=permanent,
                    duration_ms=duration_ms,
                    task_id="stagehand",
                )
            if status == "applied":
                applied += 1
            else:
                failed += 1
            console.print(f"  Result: {status} ({result.get('reason', 'no reason')})")
        except Exception as exc:
            release_lock(job)
            failed += 1
            console.print(f"[red]Stagehand failed:[/red] {exc}")

        if target_url:
            break

    console.print(f"\n[bold]Stagehand experiment done:[/bold] {applied} applied, {failed} not applied")
