#!/usr/bin/env python3
"""Generate application materials only for still-live job postings.

This wraps ApplyPilot's existing tailor/cover generation functions, but checks
each job URL before spending LLM tokens. Stale or unconfirmable postings are
skipped and the database is not changed for those rows.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, TAILORED_DIR, ensure_dirs, load_env, load_profile
from applypilot.database import get_connection, init_db
from applypilot.location import classify_location
from applypilot.scoring.cover_letter import MAX_ATTEMPTS as COVER_MAX_ATTEMPTS
from applypilot.scoring.cover_letter import generate_cover_letter
from applypilot.scoring.filenames import cover_letter_filename_stem, resume_filename_stem, unique_job_prefix
from applypilot.scoring.pdf import convert_to_pdf
from applypilot.scoring.tailor import MAX_ATTEMPTS as TAILOR_MAX_ATTEMPTS
from applypilot.scoring.tailor import tailor_resume


log = logging.getLogger("generate_active_materials")

STALE_SIGNALS = (
    "job no longer available",
    "no longer accepting applications",
    "no longer available",
    "posting has expired",
    "job posting has expired",
    "this job has expired",
    "this posting is no longer available",
    "position has been filled",
    "job has been filled",
    "job is closed",
    "job closed",
    "application deadline has passed",
    "page not found",
    "404 not found",
    "we couldn't find",
    "we couldn’t find",
)


def job_url(job: dict) -> str:
    return (job.get("application_url") or job.get("url") or "").strip()


def expected_resume_path(job: dict) -> str:
    return str(TAILORED_DIR / f"{resume_filename_stem(job)}.txt")


def expected_cover_letter_path(job: dict) -> str:
    return str(COVER_LETTER_DIR / f"{cover_letter_filename_stem(job)}.txt")


def needs_tailored_resume(job: dict) -> bool:
    return (job.get("tailored_resume_path") or "") != expected_resume_path(job)


def needs_cover_letter(job: dict) -> bool:
    return (job.get("cover_letter_path") or "") != expected_cover_letter_path(job)


def check_job_exists(client: httpx.Client, url: str) -> tuple[bool, str, str]:
    if not url:
        return False, "missing_url", "missing URL"

    response = None
    for attempt in range(3):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            return False, "unconfirmed", f"fetch failed: {exc.__class__.__name__}"

        if response.status_code != 429:
            break

        retry_after = response.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else 30.0 * (attempt + 1)
        except ValueError:
            wait = 30.0 * (attempt + 1)
        log.warning("URL check rate limited. Waiting %.0fs before retrying %s", wait, url)
        time.sleep(wait)

    if response is None:
        return False, "unconfirmed", "fetch failed"

    if response.status_code in (404, 410):
        return False, "stale", f"HTTP {response.status_code}"
    if response.status_code >= 400:
        return False, "unconfirmed", f"HTTP {response.status_code}"

    text = response.text[:250_000].lower()
    for signal in STALE_SIGNALS:
        if signal in text:
            return False, "stale", f"stale signal: {signal}"

    return True, "live", f"HTTP {response.status_code}"


def update_posting_check(conn, job: dict, posting_status: str, reason: str) -> None:
    """Persist posting liveness check metadata for later CSV review."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE jobs
        SET checked_at = ?,
            posting_status = ?,
            detail_error = CASE
                WHEN ? IN ('stale', 'unconfirmed', 'missing_url') THEN ?
                ELSE detail_error
            END
        WHERE url = ?
        """,
        (now, posting_status, posting_status, f"posting check: {reason}", job["url"]),
    )
    conn.commit()
    job["checked_at"] = now
    job["posting_status"] = posting_status


def fetch_candidates(conn, min_score: int, materials: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM jobs
        WHERE fit_score >= ?
          AND full_description IS NOT NULL
        ORDER BY fit_score DESC, discovered_at DESC
        """,
        (min_score,),
    ).fetchall()
    candidates = []
    for row in rows:
        job = dict(row)
        if not classify_location(
            job.get("location"),
            job.get("full_description") or job.get("description"),
            job.get("url"),
        ).eligible_for_generation:
            continue

        needs_resume = needs_tailored_resume(job) and (job.get("tailor_attempts") or 0) < TAILOR_MAX_ATTEMPTS
        needs_cover = needs_cover_letter(job) and (job.get("cover_attempts") or 0) < COVER_MAX_ATTEMPTS

        if materials == "resumes" and needs_resume:
            candidates.append(job)
        elif materials == "cover_letters" and needs_cover and not needs_tailored_resume(job):
            candidates.append(job)
        elif materials == "all" and (needs_resume or needs_cover):
            candidates.append(job)
    return candidates


def write_tailored_resume(conn, job: dict, resume_text: str, profile: dict, validation_mode: str) -> bool:
    tailored, report = tailor_resume(resume_text, job, profile, validation_mode=validation_mode)
    prefix = unique_job_prefix(job)
    resume_stem = resume_filename_stem(job)

    success_statuses = {"approved", "approved_with_judge_warning"}
    now = datetime.now(timezone.utc).isoformat()

    if report["status"] not in success_statuses:
        conn.execute(
            "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
            (job["url"],),
        )
        conn.commit()
        log.warning("Tailor not saved for %s: %s", job.get("title"), report["status"])
        return False

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = TAILORED_DIR / f"{resume_stem}.txt"
    txt_path.write_text(tailored, encoding="utf-8")

    job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
    company = job.get("company") or job.get("site") or ""
    job_desc = (
        f"Title: {job.get('title', '')}\n"
        f"Company: {company}\n"
        f"Location: {job.get('location', 'N/A')}\n"
        f"Score: {job.get('fit_score', 'N/A')}\n"
        f"URL: {job.get('url', '')}\n\n"
        f"{job.get('full_description', '')}"
    )
    job_path.write_text(job_desc, encoding="utf-8")

    report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    try:
        convert_to_pdf(txt_path)
    except Exception:
        log.debug("PDF generation failed for %s", txt_path, exc_info=True)

    conn.execute(
        "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
        "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
        (str(txt_path), now, job["url"]),
    )
    conn.commit()
    job["tailored_resume_path"] = str(txt_path)
    log.info("Tailored resume saved: %s", txt_path)
    return True


def write_cover_letter(conn, job: dict, resume_text: str, profile: dict, validation_mode: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()

    try:
        letter = generate_cover_letter(resume_text, job, profile, validation_mode=validation_mode)
    except Exception as exc:
        conn.execute(
            "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
            (job["url"],),
        )
        conn.commit()
        log.error("Cover letter failed for %s: %s", job.get("title"), exc)
        return False

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    cl_path = COVER_LETTER_DIR / f"{cover_letter_filename_stem(job)}.txt"
    cl_path.write_text(letter, encoding="utf-8")

    try:
        convert_to_pdf(cl_path)
    except Exception:
        log.debug("PDF generation failed for %s", cl_path, exc_info=True)

    conn.execute(
        "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
        "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
        (str(cl_path), now, job["url"]),
    )
    conn.commit()
    log.info("Cover letter saved: %s", cl_path)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--validation", choices=("strict", "normal", "lenient"), default="normal")
    parser.add_argument(
        "--materials",
        choices=("resumes", "cover_letters", "all"),
        default="all",
        help="Which material type to generate after the liveness check.",
    )
    parser.add_argument("--max-jobs", type=int, default=0, help="Optional cap for this run; 0 means no cap.")
    parser.add_argument("--sleep", type=float, default=2.0, help="Seconds to pause between URL checks.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S")

    load_env()
    ensure_dirs()
    init_db()

    conn = get_connection()
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    candidates = fetch_candidates(conn, args.min_score, args.materials)
    if args.max_jobs > 0:
        candidates = candidates[:args.max_jobs]

    log.info("Checking %d score-%d+ jobs before %s generation.", len(candidates), args.min_score, args.materials)

    stats = {
        "checked": 0,
        "skipped_stale_or_unconfirmed": 0,
        "tailored": 0,
        "cover_letters": 0,
        "tailor_failed": 0,
        "cover_failed": 0,
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
        for index, job in enumerate(candidates, start=1):
            url = job_url(job)
            exists, posting_status, reason = check_job_exists(client, url)
            update_posting_check(conn, job, posting_status, reason)
            stats["checked"] += 1
            title = job.get("title") or "(untitled)"
            log.info("%d/%d URL check [%s] %s", index, len(candidates), reason, title[:70])

            if not exists:
                stats["skipped_stale_or_unconfirmed"] += 1
                time.sleep(args.sleep)
                continue

            if args.materials in ("resumes", "all") and needs_tailored_resume(job):
                if write_tailored_resume(conn, job, resume_text, profile, args.validation):
                    stats["tailored"] += 1
                else:
                    stats["tailor_failed"] += 1

            if args.materials in ("cover_letters", "all") and not needs_tailored_resume(job) and needs_cover_letter(job):
                if write_cover_letter(conn, job, resume_text, profile, args.validation):
                    stats["cover_letters"] += 1
                else:
                    stats["cover_failed"] += 1

            time.sleep(args.sleep)

    log.info(
        "Done. checked=%d skipped=%d tailored=%d cover_letters=%d tailor_failed=%d cover_failed=%d",
        stats["checked"],
        stats["skipped_stale_or_unconfirmed"],
        stats["tailored"],
        stats["cover_letters"],
        stats["tailor_failed"],
        stats["cover_failed"],
    )


if __name__ == "__main__":
    main()
