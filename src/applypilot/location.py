"""Shared location eligibility classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from applypilot.config import load_search_config

ELIGIBLE_REMOTE = "eligible_remote"
ELIGIBLE_LOCAL = "eligible_local"
INELIGIBLE_LOCATION = "ineligible_location"
UNKNOWN_LOCATION = "unknown_location"

REMOTE_TERMS = (
    "remote",
    "work from home",
    "wfh",
    "work-from-home",
    "distributed",
    "anywhere",
    "virtual",
    "telecommute",
)

ONSITE_TERMS = ("onsite", "on-site", "hybrid", "in office", "in-office")
US_REMOTE_TERMS = (
    "remote us",
    "remote u s",
    "remote united states",
    "remote united states of america",
    "remote usa",
    "united states remote",
    "us remote",
    "u s remote",
    "usa remote",
)

# Conservative built-in transit-local coverage from Newark plus the user's
# accepted NY/NJ focus. This is intentionally text based; it avoids treating
# distant jobs as local just because they mention "United States".
LOCAL_TERMS = (
    "new york ny",
    "new york, ny",
    "new york city",
    "nyc",
    "manhattan",
    "brooklyn",
    "queens",
    "bronx",
    "staten island",
    "newark, nj",
    "newark nj",
    "newark, new jersey",
    "jersey city",
    "hoboken",
    "secaucus",
    "harrison, nj",
    "kearny, nj",
    "elizabeth, nj",
    "union, nj",
    "north bergen",
    "weehawken",
    "newport, nj",
)

NON_LOCAL_TERMS = (
    "frisco texas",
    "frisco, texas",
    "eagan minnesota",
    "remote missouri",
    "texas",
    "minnesota",
    "missouri",
    "toronto ontario",
    "germany langenfeld",
    "poland gdansk",
    "spain madrid",
    "remote india",
    "bengaluru karnataka",
    "mexico city",
    "ciudad de mexico",
    "cdmx",
    "mexico",
    "canada",
    "toronto",
    "montreal",
    "vancouver",
    "london",
    "europe",
    "germany",
    "poland",
    "spain",
    "brazil",
    "sao paulo",
    "so paulo",
    "india",
    "bengaluru",
    "philippines",
    "singapore",
    "australia",
)

NON_US_APPLICANT_TERMS = (
    "authorized to work in canada",
    "authorised to work in canada",
    "legally eligible to work in canada",
    "eligible to work in canada",
    "canadian work authorization",
    "canadian work authorisation",
    "must be based in canada",
    "applicants must be located in canada",
    "candidates must be located in canada",
    "must reside in canada",
    "canada based",
    "canada-based",
    "authorized to work in the uk",
    "authorised to work in the uk",
    "uk work authorization",
    "uk work authorisation",
    "must be based in the uk",
    "must reside in the uk",
    "authorized to work in india",
    "authorised to work in india",
    "must be based in india",
    "must reside in india",
    "authorized to work in australia",
    "authorised to work in australia",
    "must be based in australia",
    "must reside in australia",
    "authorized to work in singapore",
    "authorised to work in singapore",
    "must be based in singapore",
    "must reside in singapore",
    "authorized to work in the eu",
    "authorised to work in the eu",
    "must be based in the eu",
)


@dataclass(frozen=True)
class LocationEligibility:
    status: str
    reason: str

    @property
    def eligible_for_generation(self) -> bool:
        return self.status in {ELIGIBLE_REMOTE, ELIGIBLE_LOCAL}


def _contains_any(text: str, terms: list[str] | tuple[str, ...]) -> str | None:
    for term in terms:
        if term and term.lower() in text:
            return term
    return None


def _has_state_token(text: str, state: str) -> bool:
    return bool(re.search(rf"(^|[^a-z]){re.escape(state.lower())}([^a-z]|$)", text))


def _url_location_text(url: str | None) -> str:
    """Extract readable location tokens from job URLs such as Workday paths."""
    if not url:
        return ""
    parsed = urlparse(url)
    if "linkedin.com" in parsed.netloc and "/jobs/view/" in parsed.path:
        return ""

    path = unquote(parsed.path or "").lower()
    if "/job/" in path:
        text = path.split("/job/", 1)[1].split("/", 1)[0]
    else:
        text = path

    text = re.sub(r"[_/?=&.]+", " ", text)
    text = text.replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_location_text(text: str) -> str:
    """Normalize punctuation so terms like 'Remote, US' are matchable."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify_location(
    location: str | None,
    description: str | None = None,
    url: str | None = None,
    search_cfg: dict | None = None,
) -> LocationEligibility:
    """Classify a posting's location for the current job-search policy."""
    cfg = search_cfg or load_search_config()
    location_cfg = cfg.get("location", {})
    accept_patterns = [str(p).lower() for p in location_cfg.get("accept_patterns", [])]
    reject_patterns = [str(p).lower() for p in location_cfg.get("reject_patterns", [])]

    url_text = _url_location_text(url)
    location_evidence = _normalize_location_text(" ".join(part for part in (location or "", url_text) if part))
    combined = _normalize_location_text(" ".join(part for part in (location or "", url_text, description or "") if part))
    if not combined.strip():
        return LocationEligibility(UNKNOWN_LOCATION, "missing location")

    non_us_match = _contains_any(combined, NON_US_APPLICANT_TERMS)
    if non_us_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"non-US applicant restriction: {non_us_match}")

    reject_match = _contains_any(combined, reject_patterns)
    if reject_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"rejected by pattern: {reject_match}")

    local_match = _contains_any(location_evidence, LOCAL_TERMS)
    if local_match:
        return LocationEligibility(ELIGIBLE_LOCAL, f"local transit area: {local_match}")

    if "new jersey" in location_evidence or _has_state_token(location_evidence, "nj"):
        return LocationEligibility(ELIGIBLE_LOCAL, "New Jersey location")

    if "new york" in location_evidence or _has_state_token(location_evidence, "ny"):
        return LocationEligibility(ELIGIBLE_LOCAL, "New York location")

    non_local_match = _contains_any(location_evidence, NON_LOCAL_TERMS)
    if non_local_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"non-local location: {non_local_match}")

    if "united states of america" in location_evidence and "remote" not in location_evidence:
        return LocationEligibility(INELIGIBLE_LOCATION, "non-local United States location")

    remote_match = _contains_any(location_evidence, REMOTE_TERMS)
    if remote_match:
        constrained_remote = bool(location_evidence) and not _contains_any(location_evidence, US_REMOTE_TERMS)
        if constrained_remote and location_evidence.strip() not in {"remote"}:
            return LocationEligibility(INELIGIBLE_LOCATION, f"location-constrained remote: {location_evidence}")
        return LocationEligibility(ELIGIBLE_REMOTE, f"remote signal: {remote_match}")

    accept_match = _contains_any(location_evidence, accept_patterns)
    if accept_match and accept_match not in {"united states", "usa", "us"}:
        return LocationEligibility(ELIGIBLE_LOCAL, f"accepted by pattern: {accept_match}")

    explicit_remote_match = _contains_any(
        combined,
        (
            "remote ok",
            "remote option",
            "remote eligible",
            "fully remote",
            "100% remote",
            "work from anywhere",
        ),
    )
    if explicit_remote_match:
        return LocationEligibility(ELIGIBLE_REMOTE, f"explicit remote signal: {explicit_remote_match}")

    onsite_match = _contains_any(combined, ONSITE_TERMS)
    if onsite_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"non-local {onsite_match} role")

    return LocationEligibility(UNKNOWN_LOCATION, "no remote or local signal")


def is_location_eligible_for_discovery(
    location: str | None,
    search_cfg: dict | None = None,
    description: str | None = None,
    url: str | None = None,
) -> bool:
    """Return True when discovery should keep the posting for later stages."""
    return classify_location(location, description=description, url=url, search_cfg=search_cfg).status != INELIGIBLE_LOCATION
