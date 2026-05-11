#!/usr/bin/env python3
"""Remove tailored resumes contaminated by the `Tech |` subtitle bug."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")).expanduser()
DB_PATH = APP_DIR / "applypilot.db"
TAILORED_DIR = APP_DIR / "tailored_resumes"


def contaminated_resume_paths() -> list[Path]:
    if not TAILORED_DIR.is_dir():
        return []
    paths: list[Path] = []
    for path in sorted(TAILORED_DIR.glob("*.txt")):
        if path.name.endswith("_JOB.txt"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^Tech \|", text, flags=re.MULTILINE):
            paths.append(path)
    return paths


def cleanup(dry_run: bool) -> dict[str, int]:
    resumes = contaminated_resume_paths()
    stats = {"contaminated": len(resumes), "deleted_files": 0, "updated_rows": 0}

    delete_paths: list[Path] = []
    for resume in resumes:
        delete_paths.append(resume)
        pdf = resume.with_suffix(".pdf")
        if pdf.is_file():
            delete_paths.append(pdf)

    if DB_PATH.is_file():
        if not dry_run:
            backup_path = DB_PATH.with_suffix(f".backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
            shutil.copy2(DB_PATH, backup_path)
            print(f"database backup: {backup_path}")

        conn = sqlite3.connect(DB_PATH)
        try:
            for resume in resumes:
                resume_values = {str(resume), str(resume.resolve()), str(resume.with_suffix(".pdf"))}

                placeholders = ",".join("?" * len(resume_values))
                cur = conn.execute(
                    f"""
                    UPDATE jobs
                    SET tailored_resume_path = NULL,
                        tailored_at = NULL
                    WHERE tailored_resume_path IN ({placeholders})
                    """,
                    list(resume_values),
                )
                stats["updated_rows"] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        finally:
            conn.close()

    for path in sorted(set(delete_paths)):
        if dry_run:
            print(f"would delete {path}")
            continue
        path.unlink(missing_ok=True)
        stats["deleted_files"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = cleanup(dry_run=args.dry_run)
    print(
        "contaminated={contaminated} deleted_files={deleted_files} updated_rows={updated_rows}".format(**stats)
    )


if __name__ == "__main__":
    main()
