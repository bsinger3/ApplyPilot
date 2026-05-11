"""Filename helpers for generated job application materials."""

from __future__ import annotations

import hashlib
import re


def legacy_job_prefix(job: dict) -> str:
    """Return the original ApplyPilot site/title filename prefix."""
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "")[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job.get("site") or "")[:20].strip().replace(" ", "_")
    return f"{safe_site}_{safe_title}".strip("_")


def job_url_token(job: dict) -> str:
    """Return a short stable token for the job URL."""
    url = (job.get("url") or job.get("application_url") or "").strip()
    if not url:
        return "no_url"
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def unique_job_prefix(job: dict) -> str:
    """Return a stable per-job filename prefix.

    The legacy prefix used only site/title, which overwrote files when multiple
    postings shared the same title. Appending a URL token preserves readability
    while making filenames unique per job row.
    """
    prefix = legacy_job_prefix(job)
    token = job_url_token(job)
    return f"{prefix}_{token}" if prefix else token
