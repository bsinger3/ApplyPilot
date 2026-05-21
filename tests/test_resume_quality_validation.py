from pathlib import Path

from applypilot.scoring.validator import (
    validate_bullet_sources,
    validate_experience_subtitle_format,
    validate_resume_pdf,
    validate_title_fit,
)


PROFILE = {
    "resume_facts": {
        "preserved_companies": [
            "FriendsWithMeasurements.com",
            "ALTR",
        ],
    },
}


def test_title_fit_allows_close_title_without_noisy_suffix_copy():
    result = validate_title_fit(
        "Technical Project Manager",
        "Technical Project Manager - AI Platform Team",
        "Own cross-functional delivery for AI platform programs.",
    )

    assert result["passed"]


def test_title_fit_rejects_verbatim_noisy_title_copy():
    result = validate_title_fit(
        "Technical Project Manager - AI Platform Team",
        "Technical Project Manager - AI Platform Team",
        "Own cross-functional delivery for AI platform programs.",
    )

    assert not result["passed"]
    assert "copied verbatim" in result["errors"][0]


def test_title_fit_rejects_unrelated_title():
    result = validate_title_fit(
        "Customer Support Representative",
        "Technical Project Manager",
        "Own cross-functional delivery for AI platform programs.",
    )

    assert not result["passed"]


def test_bullet_sources_require_selected_supplemental_bullets():
    selected = {
        "ALTR": [
            {
                "bullet": "Coordinated API delivery across product, engineering, and client stakeholders.",
            }
        ]
    }
    data = {
        "experience": [
            {
                "header": "Technical Project Manager at ALTR",
                "subtitle": "Melbourne, Florida | Jan 2022 - Present",
                "bullets": [
                    "Coordinated API delivery across product, engineering, and client stakeholders.",
                    "Invented a brand-new executive analytics program.",
                ],
            }
        ]
    }

    result = validate_bullet_sources(data, selected, PROFILE)

    assert not result["passed"]
    assert "not traceable" in result["errors"][0]


def test_experience_subtitle_format_rejects_role_noise():
    data = {
        "experience": [
            {
                "header": "Technical Project Manager at ALTR",
                "subtitle": "SaaS Platform Delivery | Jan 2022 - Present",
                "bullets": [],
            }
        ]
    }

    result = validate_experience_subtitle_format(data, PROFILE)

    assert not result["passed"]
    assert "role/skill noise" in result["errors"][0]


def test_resume_pdf_rejects_multiple_pages(tmp_path: Path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n/Type /Page\n/Type /Page\n")

    result = validate_resume_pdf(pdf)

    assert not result["passed"]
    assert result["page_count"] == 2
