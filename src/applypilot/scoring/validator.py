"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from applypilot.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}

NOISY_TITLE_MARKERS: tuple[str, ...] = (" - ", " | ", ": ", "(", "[")
SUBTITLE_NOISE_TERMS: tuple[str, ...] = (
    "saas", "ai", "ml", "platform", "delivery", "implementation",
    "business systems", "data platform", "project management", "scrum",
    "stakeholder", "analytics", "technical",
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, list):
            allowed.update(s.lower().strip() for s in category)
        elif isinstance(category, set):
            allowed.update(s.lower().strip() for s in category)
    return allowed


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


def _normalize_for_match(text: object) -> str:
    """Normalize text for fuzzy validation comparisons."""
    text = str(text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: object) -> set[str]:
    """Return comparison tokens, excluding low-signal words."""
    stop = {
        "and", "the", "for", "with", "from", "that", "this", "role", "job",
        "remote", "hybrid", "onsite", "all", "genders", "team", "department",
    }
    return {token for token in _normalize_for_match(text).split() if len(token) > 2 and token not in stop}


def _similarity(left: object, right: object) -> float:
    """Return a combined token/sequence similarity score."""
    left_norm = _normalize_for_match(left)
    right_norm = _normalize_for_match(right)
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = _tokens(left_norm)
    right_tokens = _tokens(right_norm)
    token_score = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    sequence_score = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(token_score, sequence_score)


def _core_job_title(job_title: str) -> str:
    """Strip common JD suffixes while preserving the core role title."""
    title = sanitize_text(job_title)
    for marker in (" - ", " | ", ": "):
        if marker in title:
            title = title.split(marker, 1)[0]
            break
    title = re.sub(r"\s*\([^)]*\)", "", title)
    title = re.sub(r"\s*\[[^]]*\]", "", title)
    return title.strip()


def validate_title_fit(resume_title: str, job_title: str, job_description: str = "") -> dict:
    """Validate that the generated resume title is aligned without lazy-copying a noisy JD title."""
    errors: list[str] = []
    warnings: list[str] = []
    resume_title = sanitize_text(resume_title)
    job_title = sanitize_text(job_title)
    core_title = _core_job_title(job_title)

    title_score = _similarity(resume_title, core_title or job_title)
    jd_score = _similarity(resume_title, f"{job_title} {job_description[:1000]}")
    if max(title_score, jd_score) < 0.48:
        errors.append(f"Resume title '{resume_title}' is not close enough to JD title '{job_title}'.")

    resume_norm = _normalize_for_match(resume_title)
    job_norm = _normalize_for_match(job_title)
    noisy_title = any(marker in job_title for marker in NOISY_TITLE_MARKERS) or len(_tokens(job_title)) > 6
    if noisy_title and resume_norm == job_norm:
        errors.append(f"Resume title appears copied verbatim from noisy JD title: '{job_title}'.")
    elif resume_norm == job_norm and resume_norm != _normalize_for_match(core_title):
        warnings.append(f"Resume title appears copied from JD title: '{job_title}'.")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


def _selected_bullets_by_company(selected_bullets: dict | None) -> dict[str, list[dict]]:
    """Normalize selected supplemental bullet records by company name."""
    grouped: dict[str, list[dict]] = {}
    for company, bullets in (selected_bullets or {}).items():
        company_key = _normalize_for_match(company)
        grouped[company_key] = [
            bullet for bullet in bullets or []
            if str(bullet.get("bullet", "")).strip()
        ]
    return grouped


def _company_for_experience_entry(entry: dict, selected_bullets: dict | None, profile: dict) -> str:
    """Infer the canonical company for a generated experience entry."""
    haystack = f"{entry.get('header', '')} {entry.get('subtitle', '')}"
    companies: list[str] = []
    companies.extend(profile.get("resume_facts", {}).get("preserved_companies", []) or [])
    companies.extend(str(company) for company in (selected_bullets or {}).keys())
    for company in companies:
        if company and company.lower() in haystack.lower():
            return str(company)
    if " at " in str(entry.get("header", "")):
        return str(entry["header"]).split(" at ", 1)[1].strip()
    return ""


def validate_bullet_sources(data: dict, selected_bullets: dict | None, profile: dict) -> dict:
    """Validate experience bullets are selected from the supplemental bullet library."""
    errors: list[str] = []
    warnings: list[str] = []
    selected_by_company = _selected_bullets_by_company(selected_bullets)
    if not selected_by_company:
        warnings.append("No selected supplemental bullets were provided; bullet provenance was not checked.")
        return {"passed": True, "errors": errors, "warnings": warnings}

    for entry in data.get("experience", []) or []:
        company = _company_for_experience_entry(entry, selected_bullets, profile)
        company_key = _normalize_for_match(company)
        allowed = selected_by_company.get(company_key, [])
        if not allowed:
            errors.append(f"No selected supplemental bullets found for experience company '{company or '?'}'.")
            continue

        allowed_norms = {_normalize_for_match(bullet.get("bullet", "")) for bullet in allowed}
        for bullet in entry.get("bullets", []) or []:
            bullet_norm = _normalize_for_match(bullet)
            if bullet_norm in allowed_norms:
                continue
            best = max((_similarity(bullet, item.get("bullet", "")) for item in allowed), default=0.0)
            if best < 0.92:
                errors.append(
                    f"Bullet for {company} is not traceable to selected supplemental bullets: '{str(bullet)[:120]}'."
                )

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


def validate_experience_subtitle_format(data: dict, profile: dict) -> dict:
    """Validate experience subtitles stay in the deterministic location/date shape."""
    errors: list[str] = []
    warnings: list[str] = []
    preserved_companies = profile.get("resume_facts", {}).get("preserved_companies", []) or []
    date_pattern = re.compile(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}\b|"
        r"\b\d{4}\b|present|current",
        re.IGNORECASE,
    )

    for entry in data.get("experience", []) or []:
        header = sanitize_text(str(entry.get("header", "")))
        subtitle = sanitize_text(str(entry.get("subtitle", "")))
        company = _company_for_experience_entry(entry, None, profile)

        if not company:
            errors.append(f"Experience header does not include a preserved company: '{header}'.")
        elif not any(str(c).lower() == company.lower() for c in preserved_companies):
            warnings.append(f"Experience company '{company}' is not in preserved companies.")

        if not subtitle:
            errors.append(f"Experience entry for '{company or header}' is missing subtitle.")
            continue
        if subtitle.count("|") != 1:
            errors.append(
                f"Experience subtitle must be exactly 'Location | Dates' for '{company or header}': '{subtitle}'."
            )
            continue

        location, dates = [part.strip() for part in subtitle.split("|", 1)]
        if not location or not dates:
            errors.append(f"Experience subtitle has blank location or dates for '{company or header}': '{subtitle}'.")
        if company and company.lower() in subtitle.lower():
            errors.append(f"Experience subtitle should not repeat company name for '{company}': '{subtitle}'.")
        if not date_pattern.search(dates):
            errors.append(f"Experience subtitle dates are not recognizable for '{company or header}': '{dates}'.")
        location_norm = _normalize_for_match(location)
        noisy_terms = [term for term in SUBTITLE_NOISE_TERMS if term in location_norm]
        if noisy_terms:
            errors.append(
                f"Experience subtitle location contains role/skill noise for '{company or header}': '{location}'."
            )

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


def validate_resume_quality_json(
    data: dict,
    profile: dict,
    job: dict,
    selected_bullets: dict | None = None,
) -> dict:
    """Run resume-quality checks that need structured generated JSON."""
    checks = {
        "title_fit": validate_title_fit(
            str(data.get("title", "")),
            str(job.get("title", "")),
            str(job.get("full_description") or job.get("description") or ""),
        ),
        "bullet_sources": validate_bullet_sources(data, selected_bullets, profile),
        "experience_subtitles": validate_experience_subtitle_format(data, profile),
    }
    errors: list[str] = []
    warnings: list[str] = []
    for name, result in checks.items():
        errors.extend(f"{name}: {error}" for error in result.get("errors", []))
        warnings.extend(f"{name}: {warning}" for warning in result.get("warnings", []))
    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings, "checks": checks}


def pdf_page_count(path: str | Path) -> int:
    """Best-effort PDF page count without extra dependencies."""
    data = Path(path).read_bytes()
    return max(1, len(re.findall(rb"/Type\s*/Page\b", data)))


def validate_resume_pdf(path: str | Path, max_pages: int = 1) -> dict:
    """Validate rendered resume PDF page count."""
    errors: list[str] = []
    warnings: list[str] = []
    count = pdf_page_count(path)
    if count > max_pages:
        errors.append(f"Resume PDF is {count} pages; max is {max_pages}.")
    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings, "page_count": count}


# ── JSON Field Validation ─────────────────────────────────────────────────

def validate_json_fields(data: dict, profile: dict, mode: str = "normal") -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".
                 strict  → banned words are errors (trigger retries)
                 normal  → banned words are warnings (no retry)
                 lenient → banned words ignored entirely

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required keys — always checked regardless of mode
    for key in ("title", "summary", "skills", "experience", "projects", "education"):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # Collect all text for bulk checks
    all_text_parts: list[str] = [data["summary"]]

    # Skills: check for fabrication (always enforced)
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text:
                errors.append(f"Fabricated skill: '{fake}'")

    # Experience: preserved companies must be present (always enforced)
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        for company in preserved_companies:
            has_company = any(
                company.lower() in (
                    f"{e.get('header', '')} {e.get('subtitle', '')}"
                ).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Projects: collect bullets
    if isinstance(data["projects"], list):
        for entry in data["projects"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if preserved_school.lower() not in edu.lower():
            errors.append(f"Education '{preserved_school}' missing")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "SUMMARY": ["summary", "professional summary", "profile"],
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block:
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", or "lenient".
              strict  → banned words are errors (trigger retries); word limit enforced
              normal  → banned words are warnings; word limit is soft (+25 words)
              lenient → banned words ignored; word count not checked

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")
    # lenient: no word count check

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" — always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
