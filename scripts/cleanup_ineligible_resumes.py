#!/usr/bin/env python3
"""Remove tailored resumes for jobs now classified as location-ineligible."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from applypilot.location import INELIGIBLE_LOCATION, classify_location


APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")).expanduser()
DB_PATH = APP_DIR / "applypilot.db"


def cleanup(dry_run: bool = False) -> dict[str, int]:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"ApplyPilot database not found: {DB_PATH}")

    if not dry_run:
        backup_path = DB_PATH.with_suffix(f".backup-clean-ineligible-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
        shutil.copy2(DB_PATH, backup_path)
        print(f"database backup: {backup_path}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    stats = {"rows": 0, "ineligible_resumes": 0, "deleted_files": 0, "capped_scores": 0}

    try:
        rows = conn.execute(
            """
            SELECT url, title, location, description, full_description, fit_score,
                   tailored_resume_path
            FROM jobs
            WHERE tailored_resume_path IS NOT NULL
              AND tailored_resume_path != ''
            """
        ).fetchall()

        for row in rows:
            stats["rows"] += 1
            location = classify_location(
                row["location"],
                row["full_description"] or row["description"],
                row["url"],
            )
            if location.status != INELIGIBLE_LOCATION:
                continue

            stats["ineligible_resumes"] += 1
            path = Path(row["tailored_resume_path"]).expanduser()
            for candidate in (path, path.with_suffix(".pdf")):
                if candidate.is_file():
                    if dry_run:
                        print(f"would delete {candidate}")
                    else:
                        candidate.unlink()
                    stats["deleted_files"] += 1

            print(f"location-ineligible resume: {row['title']} | {location.reason}")
            if row["fit_score"] is not None and row["fit_score"] > 1:
                stats["capped_scores"] += 1

            if not dry_run:
                conn.execute(
                    """
                    UPDATE jobs
                    SET tailored_resume_path = NULL,
                        tailored_at = NULL,
                        location_status = ?,
                        fit_score = CASE
                            WHEN fit_score IS NOT NULL AND fit_score > 1 THEN 1
                            ELSE fit_score
                        END,
                        score_reasoning = TRIM(COALESCE(score_reasoning, '') || CHAR(10) || ?)
                    WHERE url = ?
                    """,
                    (location.status, f"Location ineligible: {location.reason}", row["url"]),
                )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = cleanup(args.dry_run)
    print(
        "rows={rows} ineligible_resumes={ineligible_resumes} "
        "deleted_files={deleted_files} capped_scores={capped_scores}".format(**stats)
    )


if __name__ == "__main__":
    main()
