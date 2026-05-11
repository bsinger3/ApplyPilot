#!/usr/bin/env python3
"""Backfill location_status and cap scores for location-ineligible jobs."""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from applypilot.location import INELIGIBLE_LOCATION, classify_location


APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")).expanduser()
DB_PATH = APP_DIR / "applypilot.db"


def backfill(dry_run: bool = False) -> dict[str, int]:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"ApplyPilot database not found: {DB_PATH}")

    if not dry_run:
        backup_path = DB_PATH.with_suffix(f".backup-location-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
        shutil.copy2(DB_PATH, backup_path)
        print(f"database backup: {backup_path}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    stats = {"rows": 0, "ineligible": 0, "capped_scores": 0}

    try:
        rows = conn.execute(
            """
            SELECT url, location, description, full_description, fit_score
            FROM jobs
            """
        ).fetchall()
        for row in rows:
            stats["rows"] += 1
            location = classify_location(
                row["location"],
                row["full_description"] or row["description"],
                row["url"],
            )
            if location.status == INELIGIBLE_LOCATION:
                stats["ineligible"] += 1
            if location.status == INELIGIBLE_LOCATION and row["fit_score"] is not None and row["fit_score"] > 1:
                stats["capped_scores"] += 1
                conn.execute(
                    """
                    UPDATE jobs
                    SET location_status = ?,
                        fit_score = 1,
                        score_reasoning = TRIM(COALESCE(score_reasoning, '') || CHAR(10) || ?)
                    WHERE url = ?
                    """,
                    (location.status, f"Location ineligible: {location.reason}", row["url"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET location_status = ? WHERE url = ?",
                    (location.status, row["url"]),
                )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    return stats


def main() -> None:
    stats = backfill()
    print("rows={rows} ineligible={ineligible} capped_scores={capped_scores}".format(**stats))


if __name__ == "__main__":
    main()
