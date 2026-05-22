"""Repair generated resume files in-place with deterministic sections."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from applypilot.config import resolve_persona_paths
from applypilot.scoring.pdf import convert_to_pdf
from applypilot.scoring.supplementary_bullets import load_supplementary_bullets, select_bullets_for_job
from applypilot.scoring.validator import validate_resume_pdf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESUME_DIR = (
    PROJECT_ROOT
    / "portable_applypilot"
    / "software-pm"
    / "resume_generation_unique_company_2026-05-21"
    / "resumes"
)
PORTABLE_BULLETS = (
    PROJECT_ROOT
    / "portable_applypilot"
    / "software-pm"
    / "personas"
    / "software-pm"
    / "supplementary_bullets.json"
)

LOCATIONS = {
    "FriendsWithMeasurements.com": "Newark, New Jersey",
    "ALTR": "Melbourne, Florida",
    "Guy Carpenter": "New York, New York",
    "Sirion": "New York, New York",
    "Sakhi": "New York, New York",
    "The Samaritans of New York": "New York, New York",
}
DATES = {
    "FriendsWithMeasurements.com": "Aug 2025 - Present",
    "ALTR": "Feb 2025 - Aug 2025",
    "Guy Carpenter": "Nov 2023 - Dec 2024",
    "Sirion": "May 2022 - June 2023",
    "Sakhi": "Jan 2022 - Present",
    "The Samaritans of New York": "Feb 2021 - Sep 2021",
}
DEFAULT_ROLES = {
    "FriendsWithMeasurements.com": "Technical Program Manager",
    "ALTR": "Product Manager",
    "Guy Carpenter": "Product Manager",
    "Sirion": "Technical Project Manager",
    "Sakhi": "Domestic Violence Hotline Volunteer",
    "The Samaritans of New York": "Suicide Hotline Volunteer",
}
PROJECT_BLOCK = """PROJECTS
HumaneHousing - Platform for Housing Applications
First Place Winner, Columbia University Women in Tech Hackathon | New York, New York | Oct 2022
- Co-created HumaneHousing, a prototype platform that helped recently incarcerated individuals apply for housing resources across New York State, winning first place at the Columbia University Women in Tech Hackathon and securing a $1,500 grant."""


def _load_bullet_library() -> dict[str, Any]:
    if PORTABLE_BULLETS.exists():
        return json.loads(PORTABLE_BULLETS.read_text(encoding="utf-8-sig"))
    return load_supplementary_bullets(resolve_persona_paths("software-pm"))


def _parse_job(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body_start = 0
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip()
    return {
        "title": meta.get("title", ""),
        "site": meta.get("company", ""),
        "location": meta.get("location", ""),
        "url": meta.get("url", ""),
        "full_description": "\n".join(lines[body_start:]).strip(),
    }


def _section_bounds(text: str, section: str, next_section: str) -> tuple[int, int] | None:
    start_match = re.search(rf"(?m)^{re.escape(section)}\s*$", text)
    if not start_match:
        return None
    next_match = re.search(rf"(?m)^{re.escape(next_section)}\s*$", text[start_match.end():])
    if not next_match:
        return None
    return start_match.start(), start_match.end() + next_match.start()


def _replace_section(text: str, section: str, next_section: str, replacement: str) -> str:
    bounds = _section_bounds(text, section, next_section)
    if not bounds:
        return text
    start, end = bounds
    return f"{text[:start].rstrip()}\n\n{replacement.strip()}\n\n{text[end:].lstrip()}"


def _clean_summary(text: str) -> str:
    text = text.replace("over 8 years leading", "4 years leading")
    text = text.replace("over 8 years of", "4 years of")
    text = text.replace("Proven track record in", "Experience")
    text = text.replace("Proven ability to", "Able to")
    return text


def _build_experience(selected: dict[str, list[dict[str, Any]]], counts: dict[str, int]) -> str:
    lines = ["EXPERIENCE"]
    for company in LOCATIONS:
        bullets = selected.get(company) or []
        if not bullets:
            continue
        role = str((bullets[0] or {}).get("role") or DEFAULT_ROLES[company]).strip() or DEFAULT_ROLES[company]
        lines.append(f"{role}, {company}, {LOCATIONS[company]} {DATES[company]}")
        limit = counts.get(company, 1)
        for bullet in bullets[:limit]:
            text = str(bullet.get("bullet", "")).strip()
            if text:
                lines.append(f"- {text}")
        lines.append("")
    return "\n".join(lines).strip()


def _candidate_counts() -> list[dict[str, int]]:
    return [
        {
            "FriendsWithMeasurements.com": 2,
            "ALTR": 4,
            "Guy Carpenter": 3,
            "Sirion": 3,
            "Sakhi": 1,
            "The Samaritans of New York": 1,
        },
        {
            "FriendsWithMeasurements.com": 2,
            "ALTR": 4,
            "Guy Carpenter": 3,
            "Sirion": 2,
            "Sakhi": 1,
            "The Samaritans of New York": 1,
        },
        {
            "FriendsWithMeasurements.com": 2,
            "ALTR": 3,
            "Guy Carpenter": 3,
            "Sirion": 2,
            "Sakhi": 1,
            "The Samaritans of New York": 1,
        },
        {
            "FriendsWithMeasurements.com": 2,
            "ALTR": 3,
            "Guy Carpenter": 2,
            "Sirion": 2,
            "Sakhi": 1,
            "The Samaritans of New York": 1,
        },
    ]


def _repair_text(original: str, job: dict[str, str], bullet_library: dict[str, Any], counts: dict[str, int]) -> str:
    selected = select_bullets_for_job(bullet_library, job)
    text = _clean_summary(original)
    text = _replace_section(text, "EXPERIENCE", "PROJECTS", _build_experience(selected, counts))
    text = _replace_section(text, "PROJECTS", "EDUCATION", PROJECT_BLOCK)
    return text


def repair_file(path: Path, bullet_library: dict[str, Any]) -> dict[str, Any]:
    job_path = path.with_name(f"{path.stem}_JOB.txt")
    if not job_path.exists():
        return {"path": str(path), "status": "skipped_no_job"}
    original = path.read_text(encoding="utf-8")
    job = _parse_job(job_path)
    best_text = original
    best_check: dict[str, Any] | None = None
    best_counts: dict[str, int] | None = None
    for counts in _candidate_counts():
        repaired = _repair_text(original, job, bullet_library, counts)
        path.write_text(repaired, encoding="utf-8")
        pdf = convert_to_pdf(path)
        check = validate_resume_pdf(pdf)
        if check["passed"]:
            return {"path": str(path), "status": "repaired", "pdf": str(pdf), "counts": counts, "page_count": check["page_count"]}
        best_text = repaired
        best_check = check
        best_counts = counts
    path.write_text(best_text, encoding="utf-8")
    pdf = convert_to_pdf(path)
    return {"path": str(path), "status": "repaired_over_page", "pdf": str(pdf), "counts": best_counts, "pdf_check": best_check}


def needs_repair(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "Cornell University Women in Tech Hackathon" in text:
        return True
    if "Product Manager, ALTR" in text:
        block = text.split("Product Manager, ALTR", 1)[1].split("\n\nProduct Manager, Guy Carpenter", 1)[0]
        if sum(1 for line in block.splitlines() if line.startswith("- ")) <= 2:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-dir", type=Path, default=DEFAULT_RESUME_DIR)
    parser.add_argument("--all", action="store_true", help="Repair all generated resumes with matching JOB files.")
    args = parser.parse_args()

    bullet_library = _load_bullet_library()
    files = [
        path
        for path in sorted(args.resume_dir.glob("*.txt"))
        if not path.name.endswith("_JOB.txt")
        and (args.all or needs_repair(path))
    ]
    results = [repair_file(path, bullet_library) for path in files]
    summary = {
        "resume_dir": str(args.resume_dir),
        "processed": len(results),
        "status_counts": {status: sum(1 for result in results if result["status"] == status) for status in sorted({result["status"] for result in results})},
        "results": results,
    }
    summary_path = args.resume_dir.parent / "resume_repair_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
