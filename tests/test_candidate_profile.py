"""Tests for candidate profile onboarding and prompt sanitization."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analyzer import _build_candidate_context, _build_user_prompt
from src.candidate_profile import (
    format_candidate_profile,
    format_candidate_profile_for_screening,
    generate_candidate_profile,
    redact_resume_text,
)


def test_redact_resume_text_removes_common_pii():
    text = """Jordan Ellis
Mobile: +1 555-010-2030
Email: jordan.ellis@example.com
LinkedIn: https://www.linkedin.com/in/jordan-ellis-00000000/
Experienced banking leader
"""
    redacted = redact_resume_text(text)
    assert "5550102030" not in redacted
    assert "jordan.ellis@example.com" not in redacted
    assert "linkedin.com" not in redacted.lower()
    assert "Experienced banking leader" in redacted


def test_generate_candidate_profile_builds_sanitized_summary():
    resume_text = """Jordan Ellis
Mobile: +1 555-010-2030
Email: jordan.ellis@example.com
Professional Summary
Around 16+ years of techno-functional experience in core banking product development, design, integration, deployment, implementation, architecture, and delivery.
Skill Set
Banking Domains: Deposits, Payments, Digital Banking.
Payment Systems: SWIFT, RTGS, UPI, NACH
Work Experience
IT Manager Enterprise Applications
Solution architecture, design (HLD/LLD), development, integration and implementation of core product and platform.
"""
    profile = generate_candidate_profile(
        resume_text,
        {
            "target_roles": ["Solution Architect", "Delivery", "Product Manager"],
            "seniority_preference": "manager_to_senior_manager",
        },
        ["India", "Bangalore"],
        "resume.pdf",
    )
    assert profile["years_experience"] == 16
    assert "Core Banking" in profile["core_domains"]
    assert "Payments" in profile["core_domains"]
    assert "5550102030" not in profile["resume_excerpt_redacted"]
    assert "jordan.ellis@example.com" not in profile["resume_excerpt_redacted"]
    assert profile["evidence_inventory"]["solution_architecture"] is True
    assert "FX derivatives operations" in profile["not_evidenced_directly"]


def test_candidate_context_and_prompt_use_profile_not_raw_resume():
    resume_text = """Jordan Ellis
Mobile: +1 555-010-2030
Email: jordan.ellis@example.com
16+ years in core banking architecture and delivery.
"""
    profile = generate_candidate_profile(
        resume_text,
        {"target_roles": ["Solution Architect"]},
        ["India"],
        "resume.pdf",
    )
    candidate_context = _build_candidate_context(profile, resume_text, {"target_roles": ["Solution Architect"]})
    prompt = _build_user_prompt(
        candidate_context,
        {"title": "Solution Architect", "location": "Bangalore", "company": "Visa"},
        "India, Bangalore",
        "Target roles: Solution Architect",
        "Required Qualifications: Solution architecture and banking domain knowledge",
    )
    assert "jordan.ellis@example.com" not in candidate_context
    assert "5550102030" not in candidate_context
    assert "Candidate Profile" in prompt
    assert "Important evidence rule" in prompt
    assert "Not directly evidenced" in prompt


def test_screening_profile_format_excludes_full_resume_context():
    profile = generate_candidate_profile(
        "16+ years in core banking architecture.\nWorked on payments and delivery.",
        {"target_roles": ["Solution Architect"]},
        ["India"],
        "resume.pdf",
    )
    screening = format_candidate_profile_for_screening(profile)
    scoring = format_candidate_profile(profile)
    assert "Full redacted resume context:" not in screening
    assert "Full redacted resume context:" in scoring
