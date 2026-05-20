"""Canonical profile facts and constrained title logic."""

import re

FWM_COMPANY = "FriendsWithMeasurements.com"
FWM_ROLE = "Technical Program Manager, Data & AI Pipelines"
FWM_LOCATION = "Newark, New Jersey"

_PLAUSIBLE_FWM_TITLES = (
    "Implementation Project Manager",
    "Technical Program Manager",
    "Senior Program Manager",
    "Program Manager",
    "Agile Project Manager",
    "Release Manager",
    "Technical Project Manager",
    "Project Manager",
    "Technical Product Manager",
    "Product Manager",
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def resolve_fwm_role(job_title: str = "", job_text: str = "") -> str:
    """Return a JD-aware, plausible FriendsWithMeasurements.com role title.

    Keep the title close to the target JD and avoid inventing comma subtitles.
    """
    haystack = _norm(f"{job_title} {job_text}")
    title_norm = _norm(job_title)

    for title in _PLAUSIBLE_FWM_TITLES:
        if _norm(title) == title_norm:
            return title

    if "implementation" in title_norm and "project manager" in title_norm:
        return "Implementation Project Manager"

    if "agile" in title_norm and "project manager" in title_norm and "release" in title_norm:
        return "Agile Project Manager / Release Manager"

    if "release" in title_norm and "manager" in title_norm:
        return "Release Manager"

    if "agile" in title_norm and "project manager" in title_norm:
        return "Agile Project Manager"

    if "program manager" in title_norm:
        return "Technical Program Manager"

    if "project manager" in title_norm:
        return "Technical Project Manager"

    if any(term in haystack for term in ("product manager", "product owner", "product management")):
        return "Technical Product Manager"

    if any(term in haystack for term in ("analytics", "business intelligence", "power bi", "reporting")):
        return "Technical Program Manager"

    if any(term in haystack for term in ("implementation", "delivery", "launch", "customer operation")):
        return "Implementation Project Manager"

    if any(term in haystack for term in ("scrum", "agile", "kanban")):
        return "Agile Project Manager"

    if any(term in haystack for term in ("gen ai", "genai", "llm", "nlp", "machine learning", "ml", "ai platform")):
        return "Technical Program Manager"

    if any(term in haystack for term in ("data", "pipeline", "cloud", "platform", "technical program manager")):
        return FWM_ROLE

    return FWM_ROLE


def canonicalize_experience_role(company: str, role: str, job_title: str = "", job_text: str = "") -> str:
    """Return the canonical role title for experience entries with fixed facts."""
    if company == FWM_COMPANY:
        return resolve_fwm_role(job_title=job_title, job_text=job_text)
    return role
