"""Shared location eligibility classification."""

from __future__ import annotations

import re
from dataclasses import dataclass

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

# Conservative built-in transit-local coverage from Newark plus the user's
# accepted NY/NJ focus. This is intentionally text based; it avoids treating
# distant jobs as local just because they mention "United States".
LOCAL_TERMS = (
    "new york, ny",
    "new york city",
    "nyc",
    "manhattan",
    "brooklyn",
    "queens",
    "bronx",
    "staten island",
    "newark, nj",
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
    "india",
    "philippines",
    "singapore",
    "australia",
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


def classify_location(
    location: str | None,
    description: str | None = None,
    search_cfg: dict | None = None,
) -> LocationEligibility:
    """Classify a posting's location for the current job-search policy."""
    cfg = search_cfg or load_search_config()
    location_cfg = cfg.get("location", {})
    accept_patterns = [str(p).lower() for p in location_cfg.get("accept_patterns", [])]
    reject_patterns = [str(p).lower() for p in location_cfg.get("reject_patterns", [])]

    combined = " ".join(part for part in (location or "", description or "") if part).lower()
    loc = (location or "").strip().lower()

    if not combined.strip():
        return LocationEligibility(UNKNOWN_LOCATION, "missing location")

    remote_match = _contains_any(combined, REMOTE_TERMS)
    if remote_match:
        return LocationEligibility(ELIGIBLE_REMOTE, f"remote signal: {remote_match}")

    reject_match = _contains_any(combined, reject_patterns)
    if reject_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"rejected by pattern: {reject_match}")

    non_local_match = _contains_any(loc or combined, NON_LOCAL_TERMS)
    if non_local_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"non-local location: {non_local_match}")

    local_match = _contains_any(loc or combined, LOCAL_TERMS)
    if local_match:
        return LocationEligibility(ELIGIBLE_LOCAL, f"local transit area: {local_match}")

    if "new jersey" in loc or _has_state_token(loc, "nj"):
        return LocationEligibility(ELIGIBLE_LOCAL, "New Jersey location")

    if "new york" in loc or _has_state_token(loc, "ny"):
        return LocationEligibility(ELIGIBLE_LOCAL, "New York location")

    accept_match = _contains_any(loc, accept_patterns)
    if accept_match and accept_match not in {"united states", "usa", "us"}:
        return LocationEligibility(ELIGIBLE_LOCAL, f"accepted by pattern: {accept_match}")

    onsite_match = _contains_any(combined, ONSITE_TERMS)
    if onsite_match:
        return LocationEligibility(INELIGIBLE_LOCATION, f"non-local {onsite_match} role")

    return LocationEligibility(UNKNOWN_LOCATION, "no remote or local signal")


def is_location_eligible_for_discovery(location: str | None, search_cfg: dict | None = None) -> bool:
    """Return True when discovery should keep the posting for later stages."""
    return classify_location(location, search_cfg=search_cfg).status != INELIGIBLE_LOCATION
