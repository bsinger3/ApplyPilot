"""Load and rank persona supplementary bullets for resume tailoring."""

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import PersonaPaths
from applypilot.profile_overrides import FWM_COMPANY, resolve_fwm_role

MAX_BULLETS_PER_COMPANY = 5
REDUNDANT_BULLET_JACCARD = 0.42
REDUNDANT_BULLET_CONTAINMENT = 0.62
FWM_VALUE_BULLET_ID = "fwm-base-001"


def supplementary_bullets_path(persona_paths: PersonaPaths) -> Path:
    """Return the persona-local supplementary bullets path."""
    return persona_paths.profile_path.with_name("supplementary_bullets.json")


def load_supplementary_bullets(persona_paths: PersonaPaths) -> dict[str, Any]:
    """Load a supplementary bullet library, returning an empty shape if absent."""
    path = supplementary_bullets_path(persona_paths)
    if not path.exists():
        return {
            "version": 1,
            "persona": persona_paths.slug,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "canonical_roles": {},
            "bullets": [],
        }
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_supplementary_bullets(persona_paths: PersonaPaths, library: dict[str, Any]) -> Path:
    """Write a supplementary bullet library back to the persona folder."""
    path = supplementary_bullets_path(persona_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    library["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(library, indent=2) + "\n", encoding="utf-8")
    return path


def _normalize(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _tokens(text: object) -> set[str]:
    stop = {
        "and", "the", "for", "with", "from", "that", "this", "into", "when",
        "using", "use", "uses", "job", "role", "team", "work", "will", "you",
        "your", "our", "their", "across", "within", "have", "has",
        "led", "built", "designed", "developed", "created", "managed",
        "defined", "translated", "prioritized", "coordinated", "partnered",
        "supported", "helped", "using", "including",
    }
    return {t for t in _normalize(text).split() if len(t) > 2 and t not in stop}


def bullet_has_metric(bullet: dict[str, Any]) -> bool:
    """Return whether a bullet has a concrete metric signal."""
    if bullet.get("metrics"):
        return True
    text = str(bullet.get("bullet", ""))
    return bool(
        re.search(
            r"(\$\d|(?<![A-Za-z])\d[\d,]*(?:\.\d+)?\s*(?:%|k\+|m\+|tb|hours|days|minutes|rows|products|reviews|images|contracts|columns))",
            text,
            re.IGNORECASE,
        )
    )


def score_bullet(bullet: dict[str, Any], job_text: str) -> float:
    """Score a bullet by JD keyword overlap, tags, source strength, and metrics."""
    job_tokens = _tokens(job_text)
    bullet_tokens = _tokens(bullet.get("bullet", ""))
    tag_tokens = set()
    for tag in bullet.get("tags", []) or []:
        tag_tokens.update(_tokens(tag))

    bullet_overlap = len(job_tokens & bullet_tokens)
    tag_overlap = len(job_tokens & tag_tokens)
    score = bullet_overlap * 2.0 + tag_overlap * 1.5

    if bullet_has_metric(bullet):
        score += 22.0
    if bullet.get("source_type") == "supabase_transcript":
        score += 1.0
    if bullet.get("category") == "experience":
        score += 1.0
    if bullet.get("category") in {"volunteer", "award"}:
        score -= 3.0
    return score


def _bullet_match_tokens(bullet: dict[str, Any]) -> set[str]:
    """Return content tokens used to detect near-duplicate bullets."""
    tokens = _tokens(bullet.get("bullet", ""))
    for tag in bullet.get("tags", []) or []:
        tokens.update(_tokens(tag))
    return tokens


def _jd_keyword_hits(bullet: dict[str, Any], job_text: str) -> int:
    """Count JD keyword hits across bullet text and tags."""
    job_tokens = _tokens(job_text)
    return len(job_tokens & _bullet_match_tokens(bullet))


def _bullet_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Return a redundancy similarity score for two bullet records."""
    left_tokens = _bullet_match_tokens(left)
    right_tokens = _bullet_match_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0

    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = overlap / union if union else 0.0
    containment = overlap / min(len(left_tokens), len(right_tokens))
    return max(jaccard, containment)


def bullets_are_redundant(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return whether two bullets are too similar to use together."""
    left_norm = _normalize(left.get("bullet", ""))
    right_norm = _normalize(right.get("bullet", ""))
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True

    left_tokens = _bullet_match_tokens(left)
    right_tokens = _bullet_match_tokens(right)
    if not left_tokens or not right_tokens:
        return False

    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = overlap / union if union else 0.0
    containment = overlap / min(len(left_tokens), len(right_tokens))
    return (
        jaccard >= REDUNDANT_BULLET_JACCARD
        or containment >= REDUNDANT_BULLET_CONTAINMENT
    )


def _better_redundant_bullet(left: dict[str, Any], right: dict[str, Any], job_text: str) -> dict[str, Any]:
    """Choose between redundant bullets, prioritizing JD keyword coverage."""
    def key(item: dict[str, Any]) -> tuple[float, float, int, int]:
        return (
            float(item.get("_jd_keyword_hits", _jd_keyword_hits(item, job_text))),
            float(item.get("_score", score_bullet(item, job_text))),
            1 if bullet_has_metric(item) else 0,
            len(str(item.get("bullet", ""))),
        )

    return left if key(left) >= key(right) else right


def remove_redundant_ranked_bullets(
    bullets: list[dict[str, Any]],
    job_text: str,
    max_count: int | None = None,
) -> list[dict[str, Any]]:
    """Remove near-duplicate bullets, keeping the one with stronger JD fit."""
    selected: list[dict[str, Any]] = []
    for bullet in bullets:
        replacement_index: int | None = None
        replacement: dict[str, Any] | None = None
        for index, existing in enumerate(selected):
            if not bullets_are_redundant(existing, bullet):
                continue
            better = _better_redundant_bullet(existing, bullet, job_text)
            if better is bullet:
                replacement_index = index
                replacement = better
            else:
                replacement = existing
            break

        if replacement_index is not None and replacement is bullet:
            selected[replacement_index] = bullet
        elif replacement is not None:
            continue
        else:
            selected.append(bullet)

        if max_count is not None and len(selected) >= max_count:
            break
    return selected


def _required_first_bullets(company: str, bullets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return bullets that must lead a company's experience section."""
    if company != FWM_COMPANY:
        return []
    return [
        bullet
        for bullet in bullets
        if bullet.get("id") == FWM_VALUE_BULLET_ID
    ][:1]


def _dedupe_bullets(bullets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for bullet in bullets:
        key = _normalize(bullet.get("bullet", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(bullet)
    return deduped


def upsert_supplementary_bullets(
    persona_paths: PersonaPaths,
    new_bullets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append new bullets while preserving existing bullets and deduping text."""
    library = load_supplementary_bullets(persona_paths)
    existing = library.get("bullets", [])
    library["bullets"] = _dedupe_bullets([*existing, *new_bullets])
    save_supplementary_bullets(persona_paths, library)
    return library


def select_bullets_for_job(
    bullet_library: dict[str, Any],
    job: dict[str, Any],
    max_per_company: int = MAX_BULLETS_PER_COMPANY,
) -> dict[str, list[dict[str, Any]]]:
    """Select and rank bullets per company for a target job."""
    job_text = " ".join(
        str(job.get(key, ""))
        for key in ("title", "site", "company_name", "location", "description", "full_description")
    )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bullet in _dedupe_bullets(bullet_library.get("bullets", [])):
        company = str(bullet.get("company") or "").strip()
        if not company:
            continue
        scored = dict(bullet)
        scored["_score"] = score_bullet(scored, job_text)
        scored["_jd_keyword_hits"] = _jd_keyword_hits(scored, job_text)
        if company == FWM_COMPANY:
            scored["role"] = resolve_fwm_role(
                job_title=str(job.get("title", "")),
                job_text=str(job.get("full_description") or job.get("description") or ""),
            )
        grouped[company].append(scored)

    selected: dict[str, list[dict[str, Any]]] = {}
    for company, bullets in grouped.items():
        required_first = _required_first_bullets(company, bullets)
        required_ids = {bullet.get("id") for bullet in required_first}
        rankable = [
            bullet
            for bullet in bullets
            if bullet.get("id") not in required_ids
        ]
        ranked = sorted(
            rankable,
            key=lambda item: (
                item.get("_score", 0),
                1 if bullet_has_metric(item) else 0,
                item.get("_jd_keyword_hits", 0),
                len(str(item.get("bullet", ""))),
            ),
            reverse=True,
        )
        remaining = remove_redundant_ranked_bullets(
            ranked,
            job_text=job_text,
            max_count=max_per_company - len(required_first),
        )
        selected[company] = [*required_first, *remaining]
    return selected


def enforce_required_first_experience_bullets(
    data: dict[str, Any],
    bullet_library: dict[str, Any],
) -> dict[str, Any]:
    """Force required company context bullets into generated resume data.

    FriendsWithMeasurements.com needs a value-proposition bullet first, because
    later implementation details only make sense after the product problem is
    clear to a recruiter.
    """
    bullets = bullet_library.get("bullets", []) or []
    fwm_value_bullet = next(
        (
            str(bullet.get("bullet", "")).strip()
            for bullet in bullets
            if bullet.get("id") == FWM_VALUE_BULLET_ID
            and bullet.get("company") == FWM_COMPANY
            and str(bullet.get("bullet", "")).strip()
        ),
        "",
    )
    if not fwm_value_bullet:
        return data

    for entry in data.get("experience", []) or []:
        header = f"{entry.get('header', '')} {entry.get('subtitle', '')}"
        if FWM_COMPANY.lower() not in header.lower():
            continue
        existing = [
            str(bullet).strip()
            for bullet in entry.get("bullets", []) or []
            if str(bullet).strip()
        ]
        fwm_value_norm = _normalize(fwm_value_bullet)
        entry["bullets"] = [
            fwm_value_bullet,
            *[
                bullet
                for bullet in existing
                if _normalize(bullet) != fwm_value_norm
            ],
        ]
        break
    return data


def format_selected_bullets_for_prompt(selected: dict[str, list[dict[str, Any]]]) -> str:
    """Format selected bullets for an LLM prompt."""
    lines: list[str] = []
    for company, bullets in selected.items():
        if not bullets:
            continue
        role = bullets[0].get("role", "")
        lines.append(f"{company} | {role}")
        for bullet in bullets:
            metric_marker = " [metric]" if bullet_has_metric(bullet) else ""
            lines.append(f"- {bullet['bullet']}{metric_marker}")
        lines.append("")
    return "\n".join(lines).strip()


def cap_experience_bullets(data: dict[str, Any], max_per_company: int = MAX_BULLETS_PER_COMPANY) -> dict[str, Any]:
    """Ensure no generated experience entry has more than max_per_company bullets."""
    for entry in data.get("experience", []) or []:
        bullets = entry.get("bullets")
        if isinstance(bullets, list) and len(bullets) > max_per_company:
            entry["bullets"] = bullets[:max_per_company]
    return data


def remove_redundant_experience_bullets(data: dict[str, Any], job_text: str) -> dict[str, Any]:
    """Remove redundant generated resume bullets within each experience entry."""
    for entry in data.get("experience", []) or []:
        bullet_texts = entry.get("bullets")
        if not isinstance(bullet_texts, list):
            continue
        bullet_records = [
            {
                "bullet": str(text),
                "_score": score_bullet({"bullet": str(text), "tags": []}, job_text),
                "_jd_keyword_hits": _jd_keyword_hits({"bullet": str(text), "tags": []}, job_text),
            }
            for text in bullet_texts
            if str(text).strip()
        ]
        entry["bullets"] = [
            bullet["bullet"]
            for bullet in remove_redundant_ranked_bullets(bullet_records, job_text=job_text)
        ]
    return data
