"""Tests for fast screener parsing and config model resolution."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_loader import load_config
from src.job_cache import JobCache
from src.screener import (
    _build_repair_prompt,
    _build_screen_job_summary,
    _deterministic_screen,
    _deterministic_experience_screen,
    _extract_experience_band,
    _parse_screen_response,
    build_screen_reject_analysis,
    should_bypass_premium_score,
)


def test_parse_screen_response_valid_json():
    parsed = _parse_screen_response(
        json.dumps(
            {
                "decision": "reject",
                "confidence": 0.91,
                "role_family_fit": "mismatch",
                "seniority_fit": "matched",
                "location_fit": "matched",
                "must_have_hits": ["banking domain"],
                "must_have_misses": ["fx swaps"],
                "reason": "Clear role mismatch.",
            }
        )
    )
    assert parsed["decision"] == "reject"
    assert parsed["confidence"] == 0.91
    assert parsed["must_have_misses"] == ["fx swaps"]
    assert parsed["parse_error"] == ""


def test_parse_screen_response_invalid_json_falls_back():
    parsed = _parse_screen_response("not-json")
    assert parsed["decision"] == "review"
    assert parsed["confidence"] == 0.0
    assert parsed["parse_error"]


def test_parse_screen_response_handles_wrapped_json():
    parsed = _parse_screen_response(
        """Here is the result:
```json
{"decision":"pass","confidence":0.72,"role_family_fit":"strong","seniority_fit":"matched","location_fit":"matched","must_have_hits":["banking"],"must_have_misses":[],"reason":"Looks aligned."}
```"""
    )
    assert parsed["decision"] == "pass"
    assert parsed["confidence"] == 0.72
    assert parsed["parse_error"] == ""


def test_build_repair_prompt_includes_previous_invalid_response():
    prompt = _build_repair_prompt('{"decision":"borderline"')
    assert "Previous invalid response" in prompt
    assert '{"decision":"borderline"' in prompt


def test_build_screen_job_summary_condenses_description():
    description = """Visa is a world leader in payments technology.
Join Visa and do work that matters.
About the role
We are looking for a systems administrator.
Responsibilities
- Manage IAM systems
- Provide support
Required Qualifications
- 5+ years IAM
- Active Directory
Nice to have
- Okta
"""
    summary = _build_screen_job_summary(description)
    assert "world leader in payments technology" not in summary
    assert "Join Visa and do work that matters" not in summary
    assert "About the role" in summary
    assert "Required Qualifications" in summary
    assert "Active Directory" in summary


def test_extract_experience_band_handles_range_and_plus_patterns():
    assert _extract_experience_band("Role requires 2-8 years of experience.") == (2, 8, "2-8 years", "range")
    assert _extract_experience_band("Minimum 5+ years in IAM is required.") == (5, 5, "Minimum 5+ years", "plus")


def test_deterministic_experience_screen_rejects_clearly_junior_bands():
    result = _deterministic_experience_screen(
        {"years_experience": 17},
        {
            "title": "Systems Engineer",
            "description": "Looking for candidates with 2-8 years of experience in infrastructure operations.",
        },
    )
    assert result is not None
    assert result["decision"] == "reject"
    assert result["confidence"] == 0.97
    assert result["seniority_fit"] == "overleveled"
    assert "2-8 years" in result["must_have_misses"][0]


def test_deterministic_experience_screen_allows_higher_bands_to_llm():
    result = _deterministic_experience_screen(
        {"years_experience": 17},
        {
            "title": "Technical Program Manager",
            "description": "Candidates should have 10+ years of experience in program delivery.",
        },
    )
    assert result is None


def test_deterministic_experience_screen_does_not_reject_open_ended_plus_patterns():
    result = _deterministic_experience_screen(
        {"years_experience": 17},
        {
            "title": "Engineering Manager",
            "description": "Candidates should have 7+ years of experience in software engineering leadership.",
        },
    )
    assert result is None


def test_deterministic_screen_rejects_incomplete_posting():
    result = _deterministic_screen(
        {"years_experience": 17, "evidence_inventory": {}},
        {
            "title": "Unknown",
            "description": "",
            "location": "",
        },
    )
    assert result is not None
    assert result["decision"] == "reject"
    assert "Incomplete posting" in result["must_have_misses"][0]


def test_deterministic_screen_rejects_function_mismatch():
    result = _deterministic_screen(
        {"years_experience": 17, "evidence_inventory": {}},
        {
            "title": "Sr. Business Development Leader",
            "description": "Own pipeline generation, sales targets, and business development strategy.",
        },
    )
    assert result is not None
    assert result["decision"] == "reject"
    assert "Off-target role family" in result["must_have_misses"][0]


def test_deterministic_screen_rejects_specialized_domain_not_evidenced():
    result = _deterministic_screen(
        {
            "years_experience": 17,
            "evidence_inventory": {
                "fx_operations": False,
                "middle_office_controls": False,
                "institutional_investment_ops": False,
            },
        },
        {
            "title": "Middle Office FX Analyst",
            "description": "Requires middle office controls, product control, and PnL attribution support.",
        },
    )
    assert result is not None
    assert result["decision"] == "reject"
    assert "Specialized domain not evidenced" in result["must_have_misses"][0]


def test_deterministic_screen_rejects_explicit_junior_marker():
    result = _deterministic_screen(
        {"years_experience": 17, "evidence_inventory": {}},
        {
            "title": "Associate Analyst",
            "description": "Entry level associate analyst role for recent graduates.",
        },
    )
    assert result is not None
    assert result["decision"] == "reject"
    assert result["seniority_fit"] == "overleveled"


def test_deterministic_screen_does_not_reject_mentor_junior_engineers_phrase():
    result = _deterministic_screen(
        {"years_experience": 17, "evidence_inventory": {}},
        {
            "title": "Staff Software Engineer - Data Platform",
            "description": "You will mentor junior engineers, lead design reviews, and build platform capabilities.",
        },
    )
    assert result is None


def test_load_config_resolves_fast_and_qa_models(monkeypatch):
    config_path = ROOT / "tests" / "_tmp_config_for_test.json"
    config_path.write_text(json.dumps({"companies": [], "model": "base-model"}), encoding="utf-8")
    monkeypatch.setenv("FAST_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_FAST_MODEL", "fast-model")
    monkeypatch.setenv("OPENROUTER_QA_MODEL", "qa-model")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_PREMIUM_MODEL", raising=False)

    try:
        config = load_config(str(config_path))
    finally:
        config_path.unlink(missing_ok=True)

    assert config["premium_model"] == "base-model"
    assert config["fast_model"] == "fast-model"
    assert config["qa_model"] == "qa-model"


def test_job_cache_round_trips_screening_audit_fields():
    db_path = ROOT / "tests" / "_tmp_job_cache_screen.sqlite"
    db_path.unlink(missing_ok=True)
    cache = JobCache(db_path=db_path)
    job = {
        "company": "Visa",
        "title": "Technical Product Manager",
        "location": "Bangalore",
        "job_req_id": "job-1",
        "url": "https://example.com/job-1",
        "posted_date": "2026-03-28",
        "description": "Example job description",
    }
    analysis = {
        "score": 72,
        "component_scores": {"technical_skills": 70},
        "hard_requirements_met": [],
        "hard_requirements_missing": [],
        "nice_to_have_matches": [],
        "role_family_match": "adjacent",
        "seniority_fit": "matched",
        "location_signal": "matched",
        "location_preference_applied": True,
        "parse_error": "",
        "strengths": [],
        "gaps": [],
        "verdict": "Looks reasonable.",
        "thinking": "",
        "raw_system_prompt": "premium-system",
        "raw_user_prompt": "premium-user",
        "raw_llm_content": "{}",
        "raw_llm_reasoning": "",
        "raw_llm_response_json": "{\"id\":\"resp-1\"}",
        "screen_result": {"decision": "review", "confidence": 0.62},
        "screen_system_prompt": "screen-system",
        "screen_user_prompt": "screen-user",
        "screen_raw_output": "{\"decision\":\"review\"}",
        "screen_raw_response_json": "{\"id\":\"screen-1\"}",
        "screen_model_used": "fast-model",
        "screen_attempts": [
            {
                "attempt": 1,
                "repair": False,
                "prompt": "screen-user",
                "raw_output": "{\"decision\":\"review\"}",
                "raw_response_json": "{\"id\":\"screen-1\"}",
                "parse_error": "",
                "request_error": "",
            }
        ],
        "qa_result": {},
        "qa_system_prompt": "",
        "qa_user_prompt": "",
        "qa_raw_output": "",
        "qa_raw_response_json": "",
        "qa_model_used": "",
        "prepared_job_description": "Example job description",
        "model_used": "premium-model",
        "scoring_version": "v8",
    }
    try:
        cache.save(job, cache.make_job_key(job), analysis)
        loaded, status = cache.lookup("Visa", cache.make_job_key(job), "2026-03-28", "v8")
        debug = cache.get_job_debug("job-1")
    finally:
        cache.close()
        db_path.unlink(missing_ok=True)

    assert status == "cached"
    assert loaded["screen_result"]["decision"] == "review"
    assert loaded["raw_llm_response_json"] == "{\"id\":\"resp-1\"}"
    assert loaded["screen_raw_response_json"] == "{\"id\":\"screen-1\"}"
    assert loaded["screen_system_prompt"] == "screen-system"
    assert loaded["screen_user_prompt"] == "screen-user"
    assert loaded["screen_attempts"][0]["attempt"] == 1
    assert debug["screening"]["result"]["decision"] == "review"
    assert debug["screening"]["raw_prompts"]["system"] == "screen-system"
    assert debug["screening"]["raw_model_output"]["response_json"] == "{\"id\":\"screen-1\"}"
    assert debug["raw_model_output"]["response_json"] == "{\"id\":\"resp-1\"}"
    assert debug["screening"]["attempts"][0]["repair"] is False


def test_should_bypass_premium_score_only_for_confident_rejects():
    assert should_bypass_premium_score({"decision": "reject", "confidence": 0.91}) is True
    assert should_bypass_premium_score({"decision": "reject", "confidence": 0.50}) is False
    assert should_bypass_premium_score({"decision": "review", "confidence": 0.99}) is False


def test_old_borderline_decision_is_normalized_to_review():
    parsed = _parse_screen_response(
        json.dumps(
            {
                "decision": "borderline",
                "confidence": 0.52,
                "role_family_fit": "adjacent",
                "seniority_fit": "matched",
                "location_fit": "matched",
                "must_have_hits": [],
                "must_have_misses": [],
                "reason": "Legacy naming.",
            }
        )
    )
    assert parsed["decision"] == "review"


def test_build_screen_reject_analysis_shape():
    analysis = build_screen_reject_analysis(
        {
            "decision": "reject",
            "confidence": 0.9,
            "role_family_fit": "mismatch",
            "seniority_fit": "overleveled",
            "location_fit": "matched",
            "must_have_hits": ["banking domain"],
            "must_have_misses": ["product ownership"],
            "reason": "Clearly outside target role family.",
            "raw_prompt": "screen-user",
            "raw_content": "{\"decision\":\"reject\"}",
            "raw_response_json": "{\"id\":\"screen-1\"}",
            "model_used": "fast-model",
            "screen_attempts": [{"attempt": 1, "repair": False}],
        },
        "v8",
    )
    assert analysis["score"] == 0
    assert analysis["screen_result"]["decision"] == "reject"
    assert analysis["screen_system_prompt"]
    assert analysis["screen_user_prompt"] == "screen-user"
    assert analysis["screen_model_used"] == "fast-model"
    assert analysis["screen_raw_response_json"] == "{\"id\":\"screen-1\"}"
    assert analysis["screen_attempts"][0]["attempt"] == 1
