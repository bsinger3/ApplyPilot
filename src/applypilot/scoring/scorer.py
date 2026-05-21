"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile, resolve_persona_paths
from applypilot.database import get_connection, get_jobs_by_stage, get_persona_by_slug, update_job_score
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


EXPERIENCE_CAP_SCORE = 7
EXPERIENCE_CAP_GRACE_YEARS = 3


def _parse_years_value(value: object) -> float | None:
    """Return a numeric years value from profile strings like "4" or "4 years"."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    return float(match.group())


def _format_years(years: float) -> str:
    """Render years without a trailing .0 when possible."""
    years = float(years)
    return str(int(years)) if years.is_integer() else str(years)


def _looks_like_range_continuation(prefix: str) -> bool:
    """Return whether a matched number is the upper bound of a year range."""
    return bool(re.search(r"\d+\s*(?:-|to|through)\s*$", prefix, re.IGNORECASE))


def extract_required_years(job_description: str) -> float | None:
    """Extract the highest stated minimum years-of-experience requirement.

    Job posts often list multiple skill-specific requirements. We use the
    highest minimum found so a role asking for "3+ years SQL and 8+ years PM"
    is treated as an 8-year role. For ranges like "5-7 years", the lower
    bound is the requirement.
    """
    text = re.sub(r"\s+", " ", job_description or "")
    if not text:
        return None

    found: list[float] = []
    year_unit = r"(?:years?|yrs?)"
    experience_word = r"(?:experience|professional|relevant|industry|work|background)"

    range_pattern = re.compile(
        rf"\b(?P<min>\d+(?:\.\d+)?)\s*(?:-|to|through)\s*\d+(?:\.\d+)?\s*\+?\s*{year_unit}\b"
        rf"(?=[^.;:]*\b{experience_word}\b|[^.;:]*$)",
        re.IGNORECASE,
    )
    for match in range_pattern.finditer(text):
        found.append(float(match.group("min")))

    explicit_pattern = re.compile(
        rf"\b(?:at least|minimum(?: of)?|requires?|required|need(?:s|ed)?|must have|"
        rf"looking for|bring|have|with)\b[^.;:]{{0,60}}?"
        rf"(?P<years>\d+(?:\.\d+)?)\s*\+?\s*{year_unit}\b"
        rf"(?=[^.;:]*\b{experience_word}\b|[^.;:]*$)",
        re.IGNORECASE,
    )
    for match in explicit_pattern.finditer(text):
        prefix = text[max(0, match.start("years") - 12):match.start("years")]
        if _looks_like_range_continuation(prefix):
            continue
        found.append(float(match.group("years")))

    generic_pattern = re.compile(
        rf"\b(?P<years>\d+(?:\.\d+)?)\s*\+?\s*{year_unit}\b[^.;:]{{0,80}}?\b{experience_word}\b",
        re.IGNORECASE,
    )
    for match in generic_pattern.finditer(text):
        prefix = text[max(0, match.start() - 12):match.start()]
        if _looks_like_range_continuation(prefix):
            continue
        found.append(float(match.group("years")))

    return max(found) if found else None


def apply_experience_score_cap(result: dict, job: dict, candidate_years: float | None) -> dict:
    """Cap high scores when a job asks for far more experience than the candidate has."""
    required_years = extract_required_years(job.get("full_description") or "")
    if candidate_years is None or required_years is None:
        return result

    if required_years <= candidate_years + EXPERIENCE_CAP_GRACE_YEARS:
        return result

    score = result.get("score") or 0
    if score <= EXPERIENCE_CAP_SCORE:
        return result

    capped = dict(result)
    capped["score"] = EXPERIENCE_CAP_SCORE
    cap_note = (
        f"Experience cap applied: job asks for {_format_years(required_years)} years of experience, "
        f"which is more than {_format_years(EXPERIENCE_CAP_GRACE_YEARS)} years above the candidate's "
        f"{_format_years(candidate_years)} years; capped score at {EXPERIENCE_CAP_SCORE}."
    )
    reasoning = capped.get("reasoning", "").strip()
    capped["reasoning"] = f"{cap_note} {reasoning}".strip()
    return capped


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict, candidate_years: float | None = None) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {
            "role": "user",
            "content": (
                f"CANDIDATE YEARS OF EXPERIENCE: {candidate_years if candidate_years is not None else 'unknown'}\n\n"
                f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"
            ),
        },
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        result = _parse_score_response(response)
        return apply_experience_score_cap(result, job, candidate_years)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False, persona: str = "default") -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    conn = get_connection()
    persona_row = get_persona_by_slug(persona, conn=conn)
    persona_paths = resolve_persona_paths(persona_row)
    resume_path = persona_paths.resume_path if persona else RESUME_PATH
    resume_text = resume_path.read_text(encoding="utf-8")
    profile = load_profile(persona_row)
    candidate_years = _parse_years_value(profile.get("experience", {}).get("years_of_experience_total"))

    if rescore:
        query = """
            SELECT j.id AS job_id, j.url, j.title,
                   COALESCE(j.company_name, j.site) AS site,
                   j.location, j.location_text, j.full_description
            FROM jobs j
            WHERE j.full_description IS NOT NULL
        """
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(
            conn=conn,
            stage="pending_score",
            limit=limit,
            persona_id=persona_row["id"],
        )

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job, candidate_years)
        result["url"] = job["url"]
        result["job"] = job
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # Write scores to DB
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        update_job_score(
            conn,
            r["job"],
            persona_row["id"],
            r["score"],
            f"{r['keywords']}\n{r['reasoning']}",
            now,
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM job_persona
        WHERE fit_score IS NOT NULL AND persona_id = ?
        GROUP BY fit_score ORDER BY fit_score DESC
    """, (persona_row["id"],)).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
