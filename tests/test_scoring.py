"""Tests for the analyzer Phase 2 changes."""

import sys
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analyzer import _apply_guardrails, _compute_final_score, _prepare_description


def test_compute_final_score_perfect():
    """Test a perfect 100/100 score."""
    parsed = {
        "component_scores": {
            "technical_skills": 100,
            "role_alignment": 100,
            "experience_level": 100,
            "domain_knowledge": 100,
            "education": 100,
            "location_fit": 100,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_missing": [],
    }
    assert _compute_final_score(parsed) == 100


def test_golden_match_bonus():
    """Golden match bonus should add +6 points."""
    parsed = {
        "component_scores": {
            "technical_skills": 80,
            "role_alignment": 80,
            "experience_level": 80,
            "domain_knowledge": 80,
            "education": 0,
            "location_fit": 80,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_missing": [],
    }
    assert _compute_final_score(parsed) == 86


def test_missing_reqs_penalty_cap():
    """Penalty for missing requirements should be capped at -12."""
    parsed = {
        "component_scores": {
            "technical_skills": 100,
            "role_alignment": 100,
            "experience_level": 100,
            "domain_knowledge": 100,
            "location_fit": 100,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_missing": ["a", "b", "c", "d", "e"],
    }
    assert _compute_final_score(parsed) == 94


def test_education_weight_zero():
    """Education should have 0 effect on the score."""
    base_components = {
        "technical_skills": 80,
        "role_alignment": 80,
        "experience_level": 80,
        "domain_knowledge": 80,
        "location_fit": 80,
    }

    parsed_edu_0 = {"component_scores": {**base_components, "education": 0}}
    parsed_edu_100 = {"component_scores": {**base_components, "education": 100}}

    assert _compute_final_score(parsed_edu_0) == _compute_final_score(parsed_edu_100)


def test_guardrails_penalize_off_radar_operations_role():
    """Off-radar operations titles should not stay in the Excellent band."""
    parsed = {
        "component_scores": {
            "technical_skills": 95,
            "role_alignment": 95,
            "experience_level": 95,
            "domain_knowledge": 95,
            "education": 90,
            "location_fit": 0,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_met": [],
        "hard_requirements_missing": [],
        "gaps": [],
        "verdict": "Strong fit.",
    }
    job = {"title": "Middle office ( FX)- P1- BGL"}
    resume_text = "16+ years in core banking platform delivery, payments, GL, APIs, Finacle, and transformation."
    prefs = {
        "target_roles": [
            "Solution Architect",
            "Delivery",
            "Product Manager",
            "Banking Solutions",
        ],
        "seniority_preference": "manager_to_senior_manager",
    }
    prepared_description = """Required Qualifications:
6+ months of Institutional Investment Operations experience
Job Expectations:
Middle office controls, PnL, reconciliation, and FX operations."""

    adjusted = _apply_guardrails(parsed, job, prefs, prepared_description, resume_text)

    assert adjusted["role_family_match"] == "mismatch"
    assert adjusted["guardrail_penalty"] == 20
    assert adjusted["score"] <= 25
    assert "operations/support" in " ".join(adjusted["gaps"]).lower()
    assert any("middle office or product control".lower() in item.lower() for item in adjusted["hard_requirements_missing"])


def test_guardrails_do_not_penalize_target_role_title():
    """Target-role titles should not pick up a title mismatch penalty."""
    parsed = {
        "component_scores": {
            "technical_skills": 90,
            "role_alignment": 90,
            "experience_level": 90,
            "domain_knowledge": 90,
            "education": 0,
            "location_fit": 90,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_met": ["Core banking delivery"],
        "hard_requirements_missing": [],
        "gaps": [],
        "verdict": "Strong fit.",
    }
    job = {"title": "Solution Architect - Banking Platform"}
    prefs = {
        "target_roles": [
            "Solution Architect",
            "Delivery",
            "Product Manager",
            "Banking Solutions",
        ],
        "seniority_preference": "manager_to_senior_manager",
    }

    adjusted = _apply_guardrails(
        parsed,
        job,
        prefs,
        "Required Qualifications: banking architecture",
        "15 years in solution architecture, banking platforms, and delivery leadership",
    )

    assert adjusted["guardrail_penalty"] == 0
    assert adjusted["score"] == 96


def test_guardrails_penalize_missing_specialized_resume_evidence():
    """Specialized ops requirements should be treated as missing without direct resume evidence."""
    parsed = {
        "component_scores": {
            "technical_skills": 88,
            "role_alignment": 84,
            "experience_level": 90,
            "domain_knowledge": 82,
            "education": 0,
            "location_fit": 90,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_met": [],
        "hard_requirements_missing": [],
        "gaps": [],
        "verdict": "Strong fit.",
    }

    adjusted = _apply_guardrails(
        parsed,
        {"title": "Product Control - FX Operations"},
        {"target_roles": ["Solution Architect", "Delivery", "Product Manager"]},
        "Required Qualifications: FX Options, FX Forwards, FX Swaps, daily risk & PnL, product control, institutional investment operations",
        "Core banking implementation, payments, GL integration, API delivery, Finacle",
    )

    missing = " ".join(adjusted["hard_requirements_missing"]).lower()
    assert adjusted["role_family_match"] == "mismatch"
    assert "fx derivatives" in missing
    assert "middle office or product control" in missing
    assert "pnl / attribution reporting" in missing
    assert adjusted["score"] <= 40


def test_guardrails_keep_adjacent_banking_strategy_roles_alive():
    """Adjacent but relevant banking strategy roles should not be crushed."""
    parsed = {
        "component_scores": {
            "technical_skills": 84,
            "role_alignment": 80,
            "experience_level": 88,
            "domain_knowledge": 86,
            "education": 0,
            "location_fit": 90,
        },
        "role_family_match": "strong",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "hard_requirements_met": ["Core banking transformation"],
        "hard_requirements_missing": [],
        "gaps": [],
        "verdict": "Strong fit.",
    }

    adjusted = _apply_guardrails(
        parsed,
        {"title": "Banking Product Strategy Manager"},
        {
            "target_roles": ["Solution Architect", "Delivery", "Product Manager", "Banking Solutions"],
            "seniority_preference": "manager_to_senior_manager",
        },
        "Required Qualifications: Banking product strategy, stakeholder management, platform delivery",
        "16 years in core banking platform delivery, architecture, product rollout, stakeholder management",
    )

    assert adjusted["role_family_match"] == "strong"
    assert adjusted["guardrail_penalty"] == 0
    assert adjusted["score"] >= 85


def test_prepare_description_short():
    """Short descriptions (<7000 chars) are untouched."""
    text = "Short job description."
    assert _prepare_description(text) == text


def test_prepare_description_with_requirements():
    """Long descriptions are now passed through without truncation."""
    intro = "A" * 3000
    reqs = "These are our minimum qualifications: 1. Python, 2. DevOps."
    tail = "B" * 5000

    full_text = f"{intro}\n\n{reqs}\n\n{tail}"
    assert len(full_text) > 7000

    prepared = _prepare_description(full_text)

    assert prepared == full_text


def test_prepare_description_fallback():
    """Long descriptions without markers are also passed through in full."""
    text = "A" * 5000 + "B" * 5000
    prepared = _prepare_description(text)

    assert prepared == text
