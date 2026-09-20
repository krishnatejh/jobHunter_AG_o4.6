"""Tests for the TypeSafe Jev screener and provider dispatch."""

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config_loader, screener_typesafe
from src.config_loader import get_fast_provider, load_api_key, load_config
from src.screener import should_bypass_premium_score
from src.screener_typesafe import (
    _extract_must_have_items,
    _parse_typesafe_response,
    screen_job,
)


CANDIDATE = {
    "candidate_summary_short": "Payments platform lead",
    "target_roles": ["Technical Product Manager"],
    "avoid_roles": [],
    "target_seniority": "Senior",
    "preferred_locations": ["Bangalore"],
    "core_domains": ["payments"],
    "evidence_highlights": ["Led IAM platforms"],
    "not_evidenced_directly": ["FX operations"],
    "years_experience": 17,
    "evidence_inventory": {},
}

JOB = {
    "company": "Visa",
    "title": "Technical Product Manager",
    "location": "Bangalore",
    "job_req_id": "job-1",
    "url": "https://example.com/job-1",
    "posted_date": "2026-09-01",
    "description": (
        "About the role\n"
        "We are hiring a technical product manager.\n"
        "Required Qualifications\n"
        "- 10+ years of product management experience\n"
        "- Payments domain expertise\n"
        "Nice to have\n"
        "- Rust programming\n"
    ),
}


def _typesafe_response(
    decision="review",
    confidence=0.7,
    role_family="adjacent",
    seniority="matched",
    location="matched",
    nouls=None,
):
    answers = {
        "decision": {
            "type": "choice",
            "choice": decision,
            "confidence": confidence,
            "probabilities": {},
        },
        "role_family_fit": {"type": "choice", "choice": role_family, "confidence": 0.8},
        "seniority_fit": {"type": "choice", "choice": seniority, "confidence": 0.8},
        "location_fit": {"type": "choice", "choice": location, "confidence": 0.8},
    }
    for index, value in (nouls or {}).items():
        answers[f"req_{index}"] = {"type": "noul", "noul": value}
    return {"model": "jev-1.13.0", "answers": answers, "usage": {}}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


def test_parse_typesafe_response_maps_answers_to_contract():
    parsed = _parse_typesafe_response(
        _typesafe_response(
            decision="reject",
            confidence=0.91,
            role_family="mismatch",
            seniority="overleveled",
            location="matched",
            nouls={0: 0.9, 1: 0.05},
        ),
        ["10+ years of product management", "Payments domain expertise"],
    )
    assert parsed["decision"] == "reject"
    assert parsed["confidence"] == 0.91
    assert parsed["role_family_fit"] == "mismatch"
    assert parsed["seniority_fit"] == "overleveled"
    assert parsed["must_have_hits"] == ["10+ years of product management"]
    assert parsed["must_have_misses"] == ["Payments domain expertise"]
    assert parsed["parse_error"] == ""
    assert "must-have gaps" in parsed["reason"]


def test_parse_typesafe_response_missing_decision_falls_back_to_review():
    parsed = _parse_typesafe_response({"answers": {}}, [])
    assert parsed["decision"] == "review"
    assert parsed["confidence"] == 0.0
    assert parsed["parse_error"]


def test_parse_typesafe_response_invalid_options_use_fallbacks():
    data = _typesafe_response(decision="borderline", role_family="weird")
    parsed = _parse_typesafe_response(data, [])
    assert parsed["decision"] == "review"
    assert parsed["role_family_fit"] == "adjacent"
    assert parsed["parse_error"] == ""


def test_extract_must_have_items_skips_nice_to_have_and_headings():
    items = _extract_must_have_items(str(JOB["description"]))
    assert items == [
        "10+ years of product management experience",
        "Payments domain expertise",
    ]


def test_screen_job_calls_typesafe_and_returns_result(monkeypatch):
    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["payload"] = json
        return _FakeResponse(200, _typesafe_response(decision="pass", confidence=0.66))

    monkeypatch.setattr(screener_typesafe.requests, "post", fake_post)

    result = screen_job(
        CANDIDATE,
        JOB,
        "jev-latest",
        "test-key",
        preferred_locations=["Bangalore"],
        candidate_preferences={"target_roles": ["Technical Product Manager"]},
    )

    assert calls["url"] == screener_typesafe.TYPESAFE_URL
    assert calls["headers"]["Authorization"] == "Bearer test-key"
    assert calls["payload"]["model"] == "jev-latest"
    assert "candidate_profile" in calls["payload"]["state"]
    question_keys = set(calls["payload"]["questions"].keys())
    assert {"decision", "role_family_fit", "seniority_fit", "location_fit"} <= question_keys
    assert "req_0" in question_keys and "req_1" in question_keys

    assert result["decision"] == "pass"
    assert result["confidence"] == 0.66
    assert result["model_used"] == "jev-latest"
    assert result["parse_error"] == ""
    assert result["screen_attempts"][0]["attempt"] == 1
    assert json.loads(result["raw_prompt"])["job"]["title"] == "Technical Product Manager"


def test_screen_job_short_circuits_on_deterministic_reject(monkeypatch):
    def fail_post(*args, **kwargs):
        raise AssertionError("requests.post should not be called for deterministic rejects")

    monkeypatch.setattr(screener_typesafe.requests, "post", fail_post)

    result = screen_job(
        CANDIDATE,
        {
            "company": "Acme",
            "title": "Business Development Leader",
            "location": "Bangalore",
            "description": "Own pipeline generation, sales targets, and business development strategy.",
        },
        "jev-latest",
        "test-key",
    )
    assert result["decision"] == "reject"
    assert result["model_used"] == "deterministic"


def test_screen_job_retries_on_429_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(screener_typesafe.time, "sleep", sleeps.append)

    responses = [
        _FakeResponse(429, {"error": "rate limited"}),
        _FakeResponse(200, _typesafe_response(decision="review", confidence=0.55)),
    ]

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(screener_typesafe.requests, "post", fake_post)

    result = screen_job(CANDIDATE, JOB, "jev-latest", "test-key")
    assert result["decision"] == "review"
    assert sleeps == [2]
    assert len(result["screen_attempts"]) == 2
    assert result["screen_attempts"][0]["request_error"]


def test_screen_job_fails_open_to_review_after_persistent_529(monkeypatch):
    monkeypatch.setattr(screener_typesafe.time, "sleep", lambda _s: None)

    def fake_post(*args, **kwargs):
        return _FakeResponse(529, {"error": "overloaded"})

    monkeypatch.setattr(screener_typesafe.requests, "post", fake_post)

    result = screen_job(CANDIDATE, JOB, "jev-latest", "test-key")
    assert result["decision"] == "review"
    assert result["confidence"] == 0.0
    assert "Screener request failed" in result["reason"]
    assert len(result["screen_attempts"]) == 3


def test_typesafe_reject_result_flows_through_bypass_gate():
    result = _parse_typesafe_response(
        _typesafe_response(decision="reject", confidence=0.93),
        [],
    )
    assert should_bypass_premium_score(result) is True


def test_get_fast_provider_defaults_to_openrouter(monkeypatch):
    monkeypatch.delenv("FAST_PROVIDER", raising=False)
    assert get_fast_provider() == "openrouter"
    monkeypatch.setenv("FAST_PROVIDER", "typesafe")
    assert get_fast_provider() == "typesafe"


def test_load_config_resolves_typesafe_fast_model(monkeypatch):
    monkeypatch.setenv("FAST_PROVIDER", "typesafe")
    # An OpenRouter model name must not leak into the TypeSafe call.
    monkeypatch.setenv("OPENROUTER_FAST_MODEL", "some/openrouter-model")
    monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_PREMIUM_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_QA_MODEL", raising=False)

    config_path = ROOT / "tests" / "_tmp_config_typesafe.json"
    config_path.write_text(json.dumps({"companies": [], "model": "base-model"}), encoding="utf-8")
    try:
        config = load_config(str(config_path))
    finally:
        config_path.unlink(missing_ok=True)

    assert config["fast_provider"] == "typesafe"
    assert config["fast_model"] == "jev-latest"
    assert config["premium_model"] == "base-model"

    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.13.0")
    config_path.write_text(json.dumps({"companies": [], "model": "base-model"}), encoding="utf-8")
    try:
        config = load_config(str(config_path))
    finally:
        config_path.unlink(missing_ok=True)
    assert config["fast_model"] == "jev-1.13.0"


def test_load_api_key_uses_typesafe_key_when_provider_is_typesafe(monkeypatch):
    monkeypatch.setenv("FAST_PROVIDER", "typesafe")
    monkeypatch.delenv("OPENROUTER_FAST_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-123")
    assert load_api_key("fast") == "ts-key-123"

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    try:
        load_api_key("fast")
    except EnvironmentError as exc:
        assert "TYPESAFE_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected EnvironmentError when TYPESAFE_API_KEY is missing")

    monkeypatch.setenv("FAST_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_FAST_API_KEY", "or-key-123")
    assert load_api_key("fast") == "or-key-123"
    assert config_loader.get_fast_provider() == "openrouter"
