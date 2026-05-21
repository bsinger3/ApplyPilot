from applypilot.scoring.scorer import apply_experience_score_cap, extract_required_years


def test_extract_required_years_uses_highest_minimum_requirement():
    description = """
    Requirements:
    - 3+ years of SQL experience
    - 8+ years of product management experience
    - Experience with cloud platforms
    """

    assert extract_required_years(description) == 8


def test_extract_required_years_uses_lower_bound_for_ranges():
    description = "We are looking for 5-7 years of relevant experience in operations."

    assert extract_required_years(description) == 5


def test_apply_experience_score_cap_caps_above_grace_window():
    result = {"score": 9, "keywords": "product management", "reasoning": "Strong skill match."}
    job = {"full_description": "Must have 8+ years of product management experience."}

    capped = apply_experience_score_cap(result, job, candidate_years=4)

    assert capped["score"] == 7
    assert "Experience cap applied" in capped["reasoning"]


def test_apply_experience_score_cap_keeps_score_within_grace_window():
    result = {"score": 9, "keywords": "product management", "reasoning": "Strong skill match."}
    job = {"full_description": "Must have 7+ years of product management experience."}

    capped = apply_experience_score_cap(result, job, candidate_years=4)

    assert capped == result
