"""TypeSafe Jev screener for coarse triage (used when FAST_PROVIDER=typesafe)."""

import json
import logging
import time

import requests

from src.candidate_profile import format_candidate_profile_for_screening
from src.screener import (
    _build_screen_job_summary,
    _deterministic_screen,
    _looks_like_heading,
    _normalize_screen_decision,
)

logger = logging.getLogger(__name__)

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_TYPESAFE_MODEL = "jev-latest"

MUST_HAVE_ITEM_LIMIT = 8
MUST_HAVE_HIT_THRESHOLD = 0.5

ROLE_FAMILY_OPTIONS = ("strong", "adjacent", "mismatch")
SENIORITY_OPTIONS = ("underleveled", "matched", "overleveled", "unclear")
LOCATION_OPTIONS = ("matched", "relocation_required", "mismatch", "unknown")


def screen_job(
    candidate_profile: dict,
    job: dict,
    model: str,
    api_key: str,
    preferred_locations: list[str] | None = None,
    candidate_preferences: dict | None = None,
    max_retries: int = 3,
) -> dict:
    """Run a TypeSafe Jev triage screen and return structured routing hints."""
    preferred_locations = preferred_locations or []
    candidate_preferences = candidate_preferences or {}

    deterministic = _deterministic_screen(candidate_profile, job)
    if deterministic:
        deterministic["model_used"] = "deterministic"
        deterministic["raw_prompt"] = ""
        deterministic["raw_content"] = ""
        deterministic["raw_response_json"] = ""
        return deterministic

    state = _build_state(
        candidate_profile,
        job,
        preferred_locations,
        candidate_preferences,
    )
    must_have_items = _extract_must_have_items(str(job.get("description", "") or ""))
    payload = {
        "state": state,
        "model": model,
        "questions": _build_questions(must_have_items),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    state_json = json.dumps(state, ensure_ascii=False)
    attempts: list[dict] = []
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                TYPESAFE_URL,
                headers=headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            parsed = _parse_typesafe_response(data, must_have_items)
            parsed["model_used"] = model
            parsed["raw_prompt"] = state_json
            parsed["raw_content"] = json.dumps(
                data.get("answers", {}), ensure_ascii=False
            )
            parsed["raw_response_json"] = json.dumps(data, ensure_ascii=False)
            attempts.append(
                {
                    "attempt": attempt,
                    "repair": False,
                    "prompt": state_json,
                    "raw_output": parsed["raw_content"],
                    "raw_response_json": parsed["raw_response_json"],
                    "parse_error": parsed.get("parse_error", ""),
                    "request_error": "",
                }
            )
            parsed["screen_attempts"] = list(attempts)
            return parsed
        except requests.RequestException as exc:
            last_error = exc
            status_code = (
                getattr(exc.response, "status_code", None)
                if hasattr(exc, "response")
                else None
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "repair": False,
                    "prompt": state_json,
                    "raw_output": "",
                    "raw_response_json": "",
                    "parse_error": "",
                    "request_error": str(exc),
                }
            )
            logger.warning(
                "TypeSafe screener request failed on attempt %s/%s: %s",
                attempt,
                max_retries,
                exc,
            )
            if status_code in (429, 500, 502, 503, 529) and attempt < max_retries:
                wait = 2**attempt
                logger.info("Retrying TypeSafe screener in %ss...", wait)
                time.sleep(wait)
                continue
            break

    return _default_screen_result(
        reason=f"Screener request failed: {last_error}",
        decision="review",
        confidence=0.0,
        model_used=model,
        raw_prompt=state_json,
        screen_attempts=attempts,
    )


def _build_state(
    candidate_profile: dict,
    job: dict,
    preferred_locations: list[str],
    candidate_preferences: dict,
) -> dict:
    """Build the structured state object sent to the System One endpoint."""
    preferences: dict[str, str] = {}
    if candidate_preferences.get("target_roles"):
        preferences["target_roles"] = ", ".join(candidate_preferences["target_roles"])
    avoid = (
        candidate_preferences.get("exclude_role_families")
        or candidate_preferences.get("avoid_role_families")
        or []
    )
    if avoid:
        preferences["avoid_roles"] = ", ".join(avoid)
    if candidate_preferences.get("seniority_preference"):
        preferences["preferred_seniority"] = str(
            candidate_preferences["seniority_preference"]
        )
    if not preferences:
        preferences["note"] = "No extra preferences provided."

    description = str(job.get("description", "") or "")
    return {
        "candidate_profile": format_candidate_profile_for_screening(candidate_profile),
        "candidate_preferences": preferences,
        "job": {
            "title": str(job.get("title", "N/A") or "N/A"),
            "company": str(job.get("company", "N/A") or "N/A"),
            "location": str(job.get("location", "N/A") or "N/A"),
            "preferred_locations": (
                ", ".join(preferred_locations) if preferred_locations else "Not specified"
            ),
            "description_summary": _build_screen_job_summary(description),
        },
    }


def _build_questions(must_have_items: list[str]) -> dict:
    """Build the typed question map for the System One request."""
    questions = {
        "decision": {
            "type": "choice",
            "instructions": (
                "Classify this job against the candidate profile as pass, review, "
                "or reject. Reject only on strong evidence of mismatch. If "
                "uncertain, choose review. Use the candidate profile as the source "
                "of truth for what is directly evidenced, and do not infer direct "
                "experience unless explicitly supported."
            ),
            "criteria": {
                "pass": "The role family and seniority plausibly fit the candidate's targets.",
                "review": "Fit is uncertain or mixed; evidence is not strong enough to pass or reject.",
                "reject": "Strong evidence of mismatch with the candidate's target role families or seniority.",
            },
        },
        "role_family_fit": {
            "type": "choice",
            "instructions": (
                "How well does this job's role family match the candidate's "
                "target role families? Penalize wrong role families, but do not "
                "drop plausible adjacent roles too aggressively."
            ),
            "criteria": {
                "strong": "Core role family matches the candidate's targets directly.",
                "adjacent": "Plausible neighboring role family the candidate could move into.",
                "mismatch": "Clearly outside the candidate's target role families.",
            },
        },
        "seniority_fit": {
            "type": "choice",
            "instructions": (
                "How does this job's seniority level compare to the candidate's "
                "experience and preferred seniority?"
            ),
            "criteria": {
                "underleveled": "Role is clearly more junior than the candidate.",
                "matched": "Seniority aligns with the candidate's experience.",
                "overleveled": "Role is clearly more senior than the candidate's evidenced experience.",
                "unclear": "Seniority cannot be determined from the posting.",
            },
        },
        "location_fit": {
            "type": "choice",
            "instructions": (
                "How does the job location compare to the candidate's preferred "
                "locations?"
            ),
            "criteria": {
                "matched": "Job location matches a preferred location or is remote.",
                "relocation_required": "Job location differs from preferences but is plausibly commutable or movable.",
                "mismatch": "Job location conflicts with the candidate's location preferences.",
                "unknown": "Location cannot be determined from the posting.",
            },
        },
    }
    for index, item in enumerate(must_have_items):
        questions[f"req_{index}"] = {
            "type": "noul",
            "instructions": {
                "requirement": item,
                "question": (
                    "Does the candidate profile directly evidence this job "
                    "requirement? Consider only what is explicitly supported; do "
                    "not infer direct experience."
                ),
            },
            "criteria": {
                "true": "The candidate profile explicitly evidences this requirement.",
                "false": "The candidate profile does not evidence this requirement.",
            },
        }
    return questions


def _extract_must_have_items(description: str) -> list[str]:
    """Deterministically pull requirement-like lines from the job description."""
    lines = []
    for raw_line in str(description or "").splitlines():
        cleaned = " ".join(raw_line.split()).strip()
        if cleaned:
            lines.append(cleaned)

    items: list[str] = []
    seen: set[str] = set()
    active_section = ""
    for line in lines:
        lowered = line.lower()
        if _looks_like_heading(lowered):
            active_section = lowered
            continue
        if "nice to have" in active_section:
            continue
        bullet = line.startswith(("-", "*", "•"))
        requirement_section = any(
            token in active_section
            for token in (
                "basic qualifications",
                "preferred qualifications",
                "requirements",
                "qualifications",
                "must have",
                "skills",
                "what you'll need",
                "what we are looking for",
                "what we're looking for",
            )
        )
        if not (bullet or requirement_section):
            continue
        cleaned = line.lstrip("-*•—").strip()
        if len(cleaned) < 8:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(cleaned[:160])
        if len(items) >= MUST_HAVE_ITEM_LIMIT:
            break
    return items


def _parse_typesafe_response(data: dict, must_have_items: list[str]) -> dict:
    """Convert a System One response into the screener result contract."""
    answers = data.get("answers", {}) if isinstance(data, dict) else {}

    decision_answer = answers.get("decision", {})
    decision = _normalize_screen_decision(
        decision_answer.get("choice") if isinstance(decision_answer, dict) else None
    )
    try:
        confidence = float(decision_answer.get("confidence", 0.0))
    except (TypeError, ValueError, AttributeError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if not isinstance(decision_answer, dict) or "choice" not in decision_answer:
        return _default_screen_result(
            reason="TypeSafe screener parse failure: missing decision answer",
            decision="review",
            confidence=0.0,
            parse_error="Missing decision answer in response",
        )

    role_family_fit = _normalized_option(
        answers.get("role_family_fit", {}), ROLE_FAMILY_OPTIONS, "adjacent"
    )
    seniority_fit = _normalized_option(
        answers.get("seniority_fit", {}), SENIORITY_OPTIONS, "unclear"
    )
    location_fit = _normalized_option(
        answers.get("location_fit", {}), LOCATION_OPTIONS, "unknown"
    )

    hits: list[str] = []
    misses: list[str] = []
    for index, item in enumerate(must_have_items):
        noul_answer = answers.get(f"req_{index}", {})
        try:
            noul_value = float(noul_answer.get("noul", 0.5))
        except (TypeError, ValueError, AttributeError):
            noul_value = 0.5
        if noul_value >= MUST_HAVE_HIT_THRESHOLD:
            hits.append(item)
        else:
            misses.append(item)

    reason_parts = [
        f"{decision} (confidence {confidence:.2f})",
        f"role family: {role_family_fit}",
        f"seniority: {seniority_fit}",
        f"location: {location_fit}",
    ]
    if misses:
        reason_parts.append("must-have gaps: " + "; ".join(misses[:3]))
    reason = "TypeSafe Jev triage - " + "; ".join(reason_parts)

    return {
        "decision": decision,
        "confidence": confidence,
        "role_family_fit": role_family_fit,
        "seniority_fit": seniority_fit,
        "location_fit": location_fit,
        "must_have_hits": hits,
        "must_have_misses": misses,
        "reason": reason,
        "parse_error": "",
    }


def _normalized_option(answer: object, allowed: tuple[str, ...], fallback: str) -> str:
    choice = answer.get("choice") if isinstance(answer, dict) else None
    normalized = str(choice or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _default_screen_result(
    reason: str,
    decision: str = "review",
    confidence: float = 0.0,
    parse_error: str = "",
    model_used: str = "",
    raw_prompt: str = "",
    screen_attempts: list[dict] | None = None,
) -> dict:
    return {
        "decision": _normalize_screen_decision(decision),
        "confidence": confidence,
        "role_family_fit": "adjacent",
        "seniority_fit": "unclear",
        "location_fit": "unknown",
        "must_have_hits": [],
        "must_have_misses": [],
        "reason": reason,
        "parse_error": parse_error,
        "model_used": model_used,
        "raw_prompt": raw_prompt,
        "raw_content": "",
        "raw_response_json": "",
        "screen_attempts": list(screen_attempts or []),
    }
