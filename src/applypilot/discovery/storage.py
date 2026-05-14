"""Shared discovery storage helpers."""

from __future__ import annotations

import re
import sqlite3


def clean_text(value: object) -> str | None:
    """Return a stripped string, treating pandas/JSON empty sentinels as missing."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def normalize_job_key(value: str | None) -> str:
    """Normalize company/title/location text for duplicate checks."""
    if not value:
        return ""
    text = value.lower()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"\b(inc|inc\.|llc|ltd|ltd\.|corp|corp\.|corporation|co|co\.)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _locations_compatible(a: str | None, b: str | None) -> bool:
    """Treat missing locations as compatible, otherwise require normalized equality."""
    norm_a = normalize_job_key(a)
    norm_b = normalize_job_key(b)
    return not norm_a or not norm_b or norm_a == norm_b


def find_duplicate_job(
    conn: sqlite3.Connection,
    *,
    title: str | None,
    company: str | None,
    location: str | None,
) -> sqlite3.Row | None:
    """Find an existing posting with the same normalized company/title/location."""
    title_key = normalize_job_key(title)
    company_key = normalize_job_key(company)
    if not title_key or not company_key:
        return None

    rows = conn.execute(
        """
        SELECT url, title, company, site, location, application_url
        FROM jobs
        WHERE (company IS NOT NULL AND company != '') OR strategy = 'workday_api'
        """,
    ).fetchall()
    for row in rows:
        if normalize_job_key(row["title"]) != title_key:
            continue
        existing_company = row["company"] or row["site"]
        if normalize_job_key(existing_company) != company_key:
            continue
        if _locations_compatible(row["location"], location):
            return row
    return None


def insert_discovered_job(
    conn: sqlite3.Connection,
    *,
    url: str | None,
    title: str | None,
    company: str | None,
    salary: str | None = None,
    description: str | None = None,
    location: str | None = None,
    site: str | None = None,
    strategy: str | None = None,
    discovered_at: str | None = None,
    full_description: str | None = None,
    application_url: str | None = None,
    detail_scraped_at: str | None = None,
    detail_error: str | None = None,
) -> str:
    """Insert a discovered job.

    Returns:
        "new" when inserted, "duplicate" for URL or company/title/location dupes.
    """
    url = clean_text(url)
    title = clean_text(title)
    company = clean_text(company)
    location = clean_text(location)
    application_url = clean_text(application_url)

    if not url:
        return "duplicate"

    duplicate = find_duplicate_job(conn, title=title, company=company, location=location)
    if duplicate is not None:
        if application_url and not duplicate["application_url"]:
            conn.execute(
                "UPDATE jobs SET application_url = ? WHERE url = ?",
                (application_url, duplicate["url"]),
            )
        return "duplicate"

    try:
        conn.execute(
            """
            INSERT INTO jobs (
                url, title, company, salary, description, location, site, strategy,
                discovered_at, full_description, application_url, detail_scraped_at, detail_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url,
                title,
                company,
                clean_text(salary),
                clean_text(description),
                location,
                clean_text(site),
                clean_text(strategy),
                discovered_at,
                clean_text(full_description),
                application_url,
                detail_scraped_at,
                clean_text(detail_error),
            ),
        )
    except sqlite3.IntegrityError:
        return "duplicate"
    return "new"
