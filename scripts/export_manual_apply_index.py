#!/usr/bin/env python3
"""Export jobs with generated PDFs for manual applications.

Reads the ApplyPilot SQLite database without modifying it and writes a CSV
index of jobs that have a tailored resume PDF or cover letter PDF available.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote


APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")).expanduser()
DB_PATH = APP_DIR / "applypilot.db"
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
OUTPUT_PATH = APP_DIR / "manual_apply_index.csv"

COLUMNS = (
    "job_title",
    "company",
    "score",
    "job_url",
    "tailored_resume_pdf",
    "cover_letter_pdf",
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("manual_apply_index")


def legacy_prefix(site: str | None, title: str | None) -> str:
    """Return the filename prefix used by ApplyPilot's tailor/cover stages."""
    safe_site = re.sub(r"[^\w\s-]", "", site or "")[:20].strip().replace(" ", "_")
    safe_title = re.sub(r"[^\w\s-]", "", title or "")[:50].strip().replace(" ", "_")
    return f"{safe_site}_{safe_title}".strip("_")


def unique_prefix(site: str | None, title: str | None, url: str | None) -> str:
    """Return the per-job filename prefix used by current generators."""
    prefix = legacy_prefix(site, title)
    token = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10] if url else "no_url"
    return f"{prefix}_{token}" if prefix else token


def sibling_pdf(path_value: str | None) -> Path | None:
    """Resolve a DB-stored generated path to its neighboring PDF, if present."""
    if not path_value:
        return None

    path = Path(path_value).expanduser()
    candidates = [path] if path.suffix.lower() == ".pdf" else [path.with_suffix(".pdf")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def named_pdf(directory: Path, prefix: str, suffix: str = "") -> Path | None:
    """Find a generated PDF by the ApplyPilot source/title naming convention."""
    if not prefix:
        return None

    candidate = directory / f"{prefix}{suffix}.pdf"
    if candidate.is_file():
        return candidate.resolve()
    return None


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open SQLite in read-only immutable mode so the database is not changed."""
    uri = f"file:{quote(str(db_path.resolve()))}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def export_manual_apply_index() -> int:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"ApplyPilot database not found: {DB_PATH}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with connect_readonly(DB_PATH) as conn:
        jobs = conn.execute(
            """
            SELECT
                title,
                site,
                fit_score,
                url,
                application_url,
                tailored_resume_path,
                cover_letter_path
            FROM jobs
            WHERE tailored_resume_path IS NOT NULL
               OR cover_letter_path IS NOT NULL
            ORDER BY COALESCE(fit_score, 0) DESC, site, title, url
            """
        ).fetchall()

    rows: list[dict[str, str | int | None]] = []
    for job in jobs:
        legacy = legacy_prefix(job["site"], job["title"])
        unique = unique_prefix(job["site"], job["title"], job["url"])

        resume_pdf = (
            sibling_pdf(job["tailored_resume_path"])
            or named_pdf(TAILORED_DIR, unique)
            or named_pdf(TAILORED_DIR, legacy)
        )
        cover_letter_pdf = (
            sibling_pdf(job["cover_letter_path"])
            or named_pdf(COVER_LETTER_DIR, unique, "_CL")
            or named_pdf(COVER_LETTER_DIR, legacy, "_CL")
        )

        if not resume_pdf and not cover_letter_pdf:
            continue

        rows.append(
            {
                "job_title": job["title"] or "",
                "company": job["site"] or "",
                "score": job["fit_score"] if job["fit_score"] is not None else "",
                "job_url": job["application_url"] or job["url"] or "",
                "tailored_resume_pdf": str(resume_pdf) if resume_pdf else "",
                "cover_letter_pdf": str(cover_letter_pdf) if cover_letter_pdf else "",
            }
        )

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Exported %d rows.", len(rows))
    log.info("CSV saved to %s", OUTPUT_PATH)
    return len(rows)


def main() -> None:
    export_manual_apply_index()


if __name__ == "__main__":
    main()
