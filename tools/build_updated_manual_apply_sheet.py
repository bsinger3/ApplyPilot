from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "portable_applypilot" / "software-pm"
INPUT_CSV = Path(r"C:\Users\bsing\Downloads\manual_apply_index - manual_apply_index_software-pm_with_resumes (1).csv")
OUTPUT_CSV = PORTABLE / "manual_apply_index_software-pm_refreshed.csv"
PROFILE_PATH = PORTABLE / "personas" / "software-pm" / "profile.json"
DB_PATH = PORTABLE / "applypilot.db"


REMOVAL_NOTE_RE = re.compile(
    r"applied|no longer|expired|closed|not accepting|requires video|recorded video|"
    r"additional verification|required a recorded video|new jersey was not|colombia only|"
    r"can't find another job link",
    re.IGNORECASE,
)

DOMAIN_SIGNALS = [
    "project management",
    "program management",
    "technical project",
    "technical program",
    "implementation",
    "delivery",
    "agile",
    "scrum",
    "sprint",
    "release",
    "change management",
    "risk management",
    "stakeholder",
    "cross-functional",
    "roadmap",
    "requirements",
    "jira",
    "confluence",
    "smartsheet",
    "saas",
    "software",
    "data",
    "analytics",
    "snowflake",
    "power bi",
    "crm",
    "systems",
    "automation",
    "workflow",
    "cloud",
    "ai",
    "ml",
]

NEGATIVE_TITLE_TERMS = [
    "director",
    "vice president",
    "vp ",
    "chief",
    "sales",
    "account executive",
    "field service",
    "technician",
    "engineer",
    "developer",
    "nurse",
    "tax",
    "legal counsel",
]


@dataclass(frozen=True)
class ScoreResult:
    score: int
    reasoning: str


def norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def parse_years(value: object) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def format_years(years: float) -> str:
    years = float(years)
    return str(int(years)) if years.is_integer() else str(years)


def extract_required_years(text: str) -> float | None:
    text = re.sub(r"\s+", " ", text or "")
    found: list[float] = []
    year_unit = r"(?:years?|yrs?)"
    experience_word = r"(?:experience|professional|relevant|industry|work|background)"

    range_pattern = re.compile(
        rf"\b(?P<min>\d+(?:\.\d+)?)\s*(?:-|to|through)\s*\d+(?:\.\d+)?\s*\+?\s*{year_unit}\b"
        rf"(?=[^.;:]*\b{experience_word}\b|[^.;:]*$)",
        re.IGNORECASE,
    )
    found.extend(float(match.group("min")) for match in range_pattern.finditer(text))

    explicit_pattern = re.compile(
        rf"\b(?:at least|minimum(?: of)?|requires?|required|need(?:s|ed)?|must have|"
        rf"looking for|bring|have|with)\b[^.;:]{{0,60}}?"
        rf"(?P<years>\d+(?:\.\d+)?)\s*\+?\s*{year_unit}\b"
        rf"(?=[^.;:]*\b{experience_word}\b|[^.;:]*$)",
        re.IGNORECASE,
    )
    for match in explicit_pattern.finditer(text):
        prefix = text[max(0, match.start("years") - 12):match.start("years")]
        if re.search(r"(?:-|to|through)\s*$", prefix, re.IGNORECASE):
            continue
        found.append(float(match.group("years")))

    generic_pattern = re.compile(
        rf"\b(?P<years>\d+(?:\.\d+)?)\s*\+?\s*{year_unit}\b[^.;:]{{0,80}}?\b{experience_word}\b",
        re.IGNORECASE,
    )
    for match in generic_pattern.finditer(text):
        prefix = text[max(0, match.start() - 12):match.start()]
        if re.search(r"(?:-|to|through)\s*$", prefix, re.IGNORECASE):
            continue
        found.append(float(match.group("years")))

    return max(found) if found else None


def flatten_profile_skills(profile: dict) -> list[str]:
    skills: list[str] = []
    for values in profile.get("skills_boundary", {}).values():
        if isinstance(values, list):
            skills.extend(str(v) for v in values)
    return sorted(set(skills), key=str.lower)


def title_alignment(title: str) -> float:
    t = norm(title)
    score = 0.0
    if "technical project manager" in t or "technical program manager" in t:
        score = 3.0
    elif "project manager" in t or "program manager" in t:
        score = 2.5
    elif any(term in t for term in ("delivery manager", "implementation manager", "scrum master", "product owner")):
        score = 2.0
    elif any(term in t for term in ("product manager", "business analyst", "implementation specialist")):
        score = 1.5
    elif any(term in t for term in ("manager", "lead", "coordinator")):
        score = 1.0
    if any(term in t for term in NEGATIVE_TITLE_TERMS):
        score = max(0.0, score - 1.0)
    return score


def score_job(row: dict, skills: list[str], candidate_years: float | None) -> ScoreResult:
    title = row.get("title", "")
    text = norm(" ".join([
        row.get("title", ""),
        row.get("company", ""),
        row.get("location", ""),
        row.get("score_reasoning", ""),
        row.get("full_description", ""),
        row.get("description", ""),
    ]))

    matched_skills = []
    for skill in skills:
        skill_norm = norm(skill).replace("&", "and")
        if skill_norm and skill_norm in text:
            matched_skills.append(skill)

    domain_hits = [signal for signal in DOMAIN_SIGNALS if signal in text]
    align = title_alignment(title)

    raw = 3.0
    raw += min(len(matched_skills), 14) * 0.28
    raw += min(len(domain_hits), 14) * 0.18
    raw += align

    t = norm(title)
    if align == 0:
        raw -= 2.0
    if any(term in t for term in NEGATIVE_TITLE_TERMS):
        raw -= 1.0
    if any(term in text for term in ("clearance", "ts/sci", "onsite only")):
        raw -= 2.0

    score = max(1, min(10, round(raw)))
    notes = [
        f"Deterministic refreshed score: {len(matched_skills)} profile skills mentioned",
        f"title alignment {align:.1f}/3.0",
        f"domain signals {len(domain_hits)}",
    ]
    if matched_skills:
        notes.insert(0, ", ".join(matched_skills[:18]))

    required_years = extract_required_years(text)
    if candidate_years is not None and required_years is not None and required_years > candidate_years + 3 and score > 7:
        score = 7
        notes.append(
            "Experience cap applied: job asks for "
            f"{format_years(required_years)} years, more than 3 years above candidate's "
            f"{format_years(candidate_years)} years"
        )

    return ScoreResult(score=score, reasoning="\n".join(notes))


def score_with_experience_cap(row: dict, skills: list[str], candidate_years: float | None) -> ScoreResult:
    """Preserve existing score unless the new experience cap applies."""
    base_score_text = str(row.get("score") or "").strip()
    base_reasoning = str(row.get("score_reasoning") or "").strip()
    if base_score_text.isdigit():
        score = max(1, min(10, int(base_score_text)))
        reasoning = base_reasoning
    else:
        fallback = score_job(row, skills, candidate_years)
        score = fallback.score
        reasoning = fallback.reasoning

    requirement_text = " ".join([
        row.get("Note", ""),
        row.get("title", ""),
        row.get("company", ""),
        row.get("score_reasoning", ""),
        row.get("full_description", ""),
        row.get("description", ""),
    ])
    required_years = extract_required_years(requirement_text)
    if candidate_years is not None and required_years is not None and required_years > candidate_years + 3 and score > 7:
        score = 7
        cap_note = (
            "Experience cap applied: job asks for "
            f"{format_years(required_years)} years, more than 3 years above candidate's "
            f"{format_years(candidate_years)} years; capped score at 7."
        )
        reasoning = f"{reasoning}\n{cap_note}".strip() if reasoning else cap_note

    return ScoreResult(score=score, reasoning=reasoning)


def read_csv_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_db_jobs() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT
            j.url AS source_url,
            COALESCE(j.company_name, j.site) AS company,
            j.title,
            j.location,
            j.location_text,
            j.remote_region,
            j.work_arrangement,
            j.application_url,
            j.full_description,
            j.description,
            j.discovered_at,
            jp.tailored_resume_path,
            jp.cover_letter_path,
            jp.apply_status,
            jp.apply_error,
            jp.fit_score AS score,
            jp.score_reasoning
        FROM jobs j
        LEFT JOIN personas p ON p.slug = 'software-pm'
        LEFT JOIN job_persona jp ON jp.job_id = j.id AND jp.persona_id = p.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def should_remove(row: dict) -> bool:
    note = row.get("Note") or row.get("manual_comment") or ""
    date_applied = row.get("Date Applied") or row.get("manual_applied_at") or ""
    apply_status = row.get("apply_status") or row.get("manual_apply_status") or ""
    if date_applied.strip():
        return True
    if REMOVAL_NOTE_RE.search(note):
        return True
    if re.search(r"applied|submitted|no longer|expired|closed|not accepting", apply_status, re.IGNORECASE):
        return True
    return False


def merged_row(row: dict, db_row: dict | None, fieldnames: list[str]) -> dict:
    out = {field: row.get(field, "") for field in fieldnames}
    if db_row:
        out["title"] = out.get("title") or db_row.get("title") or ""
        out["company"] = out.get("company") or db_row.get("company") or ""
        out["source_url"] = out.get("source_url") or db_row.get("source_url") or ""
        out["location"] = out.get("location") or db_row.get("location") or db_row.get("location_text") or ""
        out["application_url"] = out.get("application_url") or db_row.get("application_url") or ""
        out["work_arrangement"] = out.get("work_arrangement") or db_row.get("work_arrangement") or ""
        out["remote_region"] = out.get("remote_region") or db_row.get("remote_region") or ""
        out["discovered_at"] = out.get("discovered_at") or db_row.get("discovered_at") or ""
        out["tailored_resume_path"] = out.get("tailored_resume_path") or db_row.get("tailored_resume_path") or ""
        out["cover_letter_path"] = out.get("cover_letter_path") or db_row.get("cover_letter_path") or ""
        out["score"] = out.get("score") or str(db_row.get("score") or "")
        out["score_reasoning"] = out.get("score_reasoning") or db_row.get("score_reasoning") or ""
        out["_full_description"] = db_row.get("full_description") or db_row.get("description") or ""
    return out


def main() -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8-sig"))
    skills = flatten_profile_skills(profile)
    candidate_years = parse_years(profile.get("experience", {}).get("years_of_experience_total"))

    input_rows = read_csv_rows(INPUT_CSV)
    fieldnames = list(input_rows[0].keys())
    if "link_check_status" not in fieldnames:
        fieldnames.extend(["link_check_status", "link_check_note"])

    db_jobs = load_db_jobs()
    db_by_url = {row.get("source_url"): row for row in db_jobs if row.get("source_url")}
    input_urls = {row.get("source_url") for row in input_rows if row.get("source_url")}

    output: list[dict] = []
    removed = 0
    for row in input_rows:
        if should_remove(row):
            removed += 1
            continue
        url = row.get("source_url")
        out = merged_row(row, db_by_url.get(url), fieldnames)
        out["full_description"] = out.pop("_full_description", "")
        score = score_with_experience_cap(out, skills, candidate_years)
        out["score"] = str(score.score)
        out["score_reasoning"] = score.reasoning
        out["link_check_status"] = "not_checked_network_restricted"
        out["link_check_note"] = "Not finalized: outbound link checking was blocked in sandbox."
        output.append({field: out.get(field, "") for field in fieldnames})

    added_from_db = 0
    for db_row in db_jobs:
        url = db_row.get("source_url")
        if not url or url in input_urls:
            continue
        base = {field: "" for field in fieldnames}
        out = merged_row(base, db_row, fieldnames)
        out["full_description"] = out.pop("_full_description", "")
        if should_remove(out):
            continue
        score = score_with_experience_cap(out, skills, candidate_years)
        out["score"] = str(score.score)
        out["score_reasoning"] = score.reasoning
        out["link_check_status"] = "not_checked_network_restricted"
        out["link_check_note"] = "Added from ApplyPilot DB; outbound link checking was blocked in sandbox."
        output.append({field: out.get(field, "") for field in fieldnames})
        added_from_db += 1

    output.sort(
        key=lambda row: (
            int(row.get("score") or 0),
            row.get("discovered_at") or "",
            row.get("title") or "",
        ),
        reverse=True,
    )

    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output)

    print(json.dumps({
        "input_rows": len(input_rows),
        "removed_by_notes": removed,
        "added_from_db": added_from_db,
        "output_rows": len(output),
        "output_csv": str(OUTPUT_CSV),
    }, indent=2))


if __name__ == "__main__":
    main()
