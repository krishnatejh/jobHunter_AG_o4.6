"""Fast LLM screener for coarse triage before premium scoring."""

import json
import logging
import re
import time

import requests

from src.candidate_profile import format_candidate_profile_for_screening

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SCREEN_SYSTEM_PROMPT = """You are a fast but careful job screener.

Your task is triage only. Do not assign a final numeric fit score.
Classify the job as PASS, REVIEW, or REJECT.

Rules:
1. Reject only on strong evidence of mismatch.
2. If uncertain, choose REVIEW, not REJECT.
3. Use the candidate profile as the source of truth for what is directly evidenced.
4. Do not infer direct experience unless explicitly supported.
5. Penalize wrong role family and wrong seniority, but do not drop plausible adjacent roles too aggressively.

Return ONLY valid JSON in this exact shape:
{
  "decision": "pass|review|reject",
  "confidence": <0.0-1.0>,
  "role_family_fit": "strong|adjacent|mismatch",
  "seniority_fit": "underleveled|matched|overleveled|unclear",
  "location_fit": "matched|relocation_required|mismatch|unknown",
  "must_have_hits": ["item1", "item2"],
  "must_have_misses": ["item1", "item2"],
  "reason": "one short paragraph"
}
"""

SCREEN_REJECT_CONFIDENCE = 0.85

SCREEN_REPAIR_PROMPT_TEMPLATE = """Your previous response was not valid JSON for the required schema.

Return ONLY valid JSON that matches the exact schema from the system instructions.
Do not include markdown fences, explanation, or any text outside the JSON.

Previous invalid response:
{previous_response}"""


def screen_job(
    candidate_profile: dict,
    job: dict,
    model: str,
    api_key: str,
    preferred_locations: list[str] | None = None,
    candidate_preferences: dict | None = None,
    max_retries: int = 3,
) -> dict:
    """Run a fast triage screen and return structured routing hints."""
    preferred_locations = preferred_locations or []
    candidate_preferences = candidate_preferences or {}

    deterministic = _deterministic_screen(candidate_profile, job)
    if deterministic:
        deterministic["model_used"] = "deterministic"
        deterministic["raw_prompt"] = ""
        deterministic["raw_content"] = ""
        deterministic["raw_response_json"] = ""
        return deterministic

    user_prompt = _build_screen_prompt(
        candidate_profile,
        job,
        preferred_locations,
        candidate_preferences,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jobhunter-agent",
        "X-Title": "Job Hunter Agent Screener",
    }
    current_user_prompt = user_prompt
    payload = _build_screen_payload(model, current_user_prompt)
    attempts: list[dict] = []

    last_error = None

    for attempt in range(1, max_retries + 1):
        is_repair = attempt > 1
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            content = _extract_message_text(
                data.get("choices", [{}])[0].get("message", {}).get("content")
            )
            parsed = _parse_screen_response(content)
            parsed["model_used"] = model
            parsed["raw_prompt"] = current_user_prompt
            parsed["raw_content"] = content
            parsed["raw_response_json"] = json.dumps(data, ensure_ascii=False)
            attempts.append(
                {
                    "attempt": attempt,
                    "repair": is_repair,
                    "prompt": current_user_prompt,
                    "raw_output": content,
                    "raw_response_json": parsed["raw_response_json"],
                    "parse_error": parsed.get("parse_error", ""),
                    "request_error": "",
                }
            )
            parsed["screen_attempts"] = list(attempts)
            if not parsed.get("parse_error"):
                return parsed
            if attempt < max_retries:
                logger.warning(
                    "Fast screener parse failure on attempt %s/%s; retrying with JSON repair prompt",
                    attempt,
                    max_retries,
                )
                current_user_prompt = _build_repair_prompt(content)
                payload = _build_screen_payload(model, current_user_prompt, repair=True)
                continue
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
                    "repair": is_repair,
                    "prompt": current_user_prompt,
                    "raw_output": "",
                    "raw_response_json": "",
                    "parse_error": "",
                    "request_error": str(exc),
                }
            )
            logger.warning("Fast screener request failed on attempt %s/%s: %s", attempt, max_retries, exc)
            if status_code in (429, 500, 502, 503) and attempt < max_retries:
                wait = 2 ** attempt
                logger.info("Retrying fast screener in %ss...", wait)
                time.sleep(wait)
                continue
            break

    return _default_screen_result(
        reason=f"Screener request failed: {last_error}",
        decision="review",
        confidence=0.0,
        model_used=model,
        raw_prompt=current_user_prompt,
        screen_attempts=attempts,
    )


def _build_screen_payload(model: str, user_prompt: str, repair: bool = False) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SCREEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 1200 if not repair else 1600,
    }


def _build_screen_prompt(
    candidate_profile: dict,
    job: dict,
    preferred_locations: list[str],
    candidate_preferences: dict,
) -> str:
    """Build a concise triage prompt from the candidate profile and job."""
    candidate_context = format_candidate_profile_for_screening(candidate_profile)
    preferences_text = []
    if candidate_preferences.get("target_roles"):
        preferences_text.append(
            "Target roles: " + ", ".join(candidate_preferences.get("target_roles", []))
        )
    avoid = candidate_preferences.get("exclude_role_families") or candidate_preferences.get("avoid_role_families") or []
    if avoid:
        preferences_text.append("Avoid roles: " + ", ".join(avoid))
    if candidate_preferences.get("seniority_preference"):
        preferences_text.append(
            "Preferred seniority: " + str(candidate_preferences.get("seniority_preference"))
        )

    description = str(job.get("description", "") or "")
    job_summary = _build_screen_job_summary(description)
    return f"""## Candidate Profile
{candidate_context}

## Candidate Preferences
{chr(10).join(preferences_text) if preferences_text else "No extra preferences provided."}

## Job
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Location: {job.get('location', 'N/A')}
Preferred locations: {', '.join(preferred_locations) if preferred_locations else 'Not specified'}

Job summary for triage:
{job_summary}
"""


def _build_screen_job_summary(description: str) -> str:
    """Condense the job description for the fast triage stage."""
    lines = []
    for raw_line in description.splitlines():
        cleaned = " ".join(raw_line.split()).strip()
        if cleaned:
            lines.append(cleaned)
    if not lines:
        return "No description available."

    scored_lines = []
    active_section = ""
    for index, line in enumerate(lines):
        lowered = line.lower()
        if _looks_like_heading(lowered):
            active_section = lowered
        score = _screen_line_score(line, lowered, active_section, index)
        if score <= -100:
            continue
        scored_lines.append((score, index, line))

    if not scored_lines:
        return "\n".join(lines[:12])

    scored_lines.sort(key=lambda item: (-item[0], item[1]))
    selected = []
    seen = set()
    for score, index, line in scored_lines:
        if line in seen:
            continue
        selected.append((index, line))
        seen.add(line)
        if len(selected) >= 16:
            break

    selected.sort(key=lambda item: item[0])
    summary_lines = [line for _, line in selected]
    if not any(_is_requirement_like(line.lower()) for line in summary_lines):
        summary_lines = lines[: min(12, len(lines))]
    return "\n".join(summary_lines)


def _deterministic_screen(candidate_profile: dict, job: dict) -> dict | None:
    """Apply explicit high-confidence reject rules before the fast LLM."""
    for checker in (
        _deterministic_incomplete_posting_screen,
        _deterministic_function_mismatch_screen,
        _deterministic_specialized_domain_screen,
        _deterministic_experience_screen,
    ):
        result = checker(candidate_profile, job)
        if result:
            return result
    return None


def _deterministic_incomplete_posting_screen(candidate_profile: dict, job: dict) -> dict | None:
    """Reject jobs that are too incomplete to judge reliably."""
    title = str(job.get("title", "") or "").strip()
    description = str(job.get("description", "") or "").strip()
    location = str(job.get("location", "") or "").strip()

    missing = []
    if not title or title.lower() in {"unknown", "n/a"}:
        missing.append("title")
    if len(description) < 40:
        missing.append("description")

    if not missing:
        return None

    return _deterministic_reject(
        reason=(
            "Posting is too incomplete for reliable screening because it is missing "
            + " and ".join(missing)
            + "."
        ),
        misses=[f"Incomplete posting: missing {item}" for item in missing],
        seniority_fit="unclear",
        location_fit="unknown" if not location else "matched",
    )


def _deterministic_function_mismatch_screen(candidate_profile: dict, job: dict) -> dict | None:
    """Reject explicit non-target job families when the title/JD is unambiguous."""
    text = " ".join(
        str(part or "")
        for part in (
            job.get("title", ""),
            job.get("description", ""),
        )
    ).lower()
    title = str(job.get("title", "") or "").lower()

    mismatch_groups = [
        ("sales/business development", ("business development", "sales leader", "account executive", "quota", "pipeline generation"), 2),
        ("finance/accounting", ("contract accounting", "financial analyst", "accounting close", "controllership", "general accounting"), 2),
        ("cybersecurity operations", ("cybersecurity analyst", "soc analyst", "iam administrator", "identity and access management", "incident response"), 2),
        ("treasury/analyst operations", ("treasury analyst", "rbac", "user access management", "role based access control"), 2),
        ("human resources", ("human resources", "recruiter", "talent acquisition", "people partner"), 1),
    ]
    for label, markers, threshold in mismatch_groups:
        hits = sum(1 for marker in markers if marker in text)
        if hits >= threshold or any(marker in title for marker in markers[:1]):
            return _deterministic_reject(
                reason=f"The role is an explicit {label} function, which is outside the candidate's target role families.",
                misses=[f"Off-target role family: {label}"],
            )
    return None


def _deterministic_specialized_domain_screen(candidate_profile: dict, job: dict) -> dict | None:
    """Reject specialized operational domains that are explicitly not evidenced."""
    text = " ".join(
        str(part or "")
        for part in (
            job.get("title", ""),
            job.get("description", ""),
        )
    ).lower()
    evidence = candidate_profile.get("evidence_inventory", {}) or {}
    domain_rules = [
        (
            "FX derivatives operations",
            ("fx option", "fx options", "fx forward", "fx forwards", "fx swaps", "foreign exchange derivatives"),
            "fx_operations",
        ),
        (
            "Middle office / product control",
            ("middle office", "product control", "pnl attribution", "profit and loss attribution"),
            "middle_office_controls",
        ),
        (
            "Institutional investment operations",
            ("institutional investment operations", "institutional trades", "isda"),
            "institutional_investment_ops",
        ),
    ]
    for label, markers, key in domain_rules:
        hits = sum(1 for marker in markers if marker in text)
        if hits >= 2 and not evidence.get(key, False):
            return _deterministic_reject(
                reason=f"The role requires specialized {label}, which is explicitly not evidenced in the candidate profile.",
                misses=[f"Specialized domain not evidenced: {label}"],
            )
    return None


def _deterministic_experience_screen(candidate_profile: dict, job: dict) -> dict | None:
    """Reject clearly under-leveled roles from explicit experience bands and junior markers."""
    candidate_years = _safe_int(candidate_profile.get("years_experience", 0))
    if candidate_years < 12:
        return None

    text = " ".join(
        str(part or "")
        for part in (
            job.get("title", ""),
            job.get("description", ""),
        )
    ).lower()
    band = _extract_experience_band(text)
    if band:
        min_years, max_years, source, match_type = band
        if match_type == "range" and max_years and max_years <= 8:
            return _deterministic_reject(
                reason=(
                    f"Explicit experience band `{source}` is clearly below the candidate's seniority "
                    f"({candidate_years}+ years), so the role is rejected before LLM screening."
                ),
                misses=[f"Experience band too junior: {source}"],
                seniority_fit="overleveled",
            )

    title = str(job.get("title", "") or "").lower()
    if _has_explicit_junior_role_marker(title, text):
        return _deterministic_reject(
            reason="The role contains explicit junior/entry-level markers that are clearly below the candidate's seniority.",
            misses=["Explicit junior marker in posting"],
            seniority_fit="overleveled",
        )
    return None


def _deterministic_reject(
    reason: str,
    misses: list[str],
    seniority_fit: str = "matched",
    location_fit: str = "unknown",
) -> dict:
    return _default_screen_result(
        reason=reason,
        decision="reject",
        confidence=0.97,
        model_used="deterministic",
        raw_prompt="",
        raw_content="",
        raw_response_json="",
    ) | {
        "role_family_fit": "mismatch",
        "seniority_fit": seniority_fit,
        "location_fit": location_fit,
        "must_have_hits": [],
        "must_have_misses": misses,
    }


def _has_explicit_junior_role_marker(title: str, text: str) -> bool:
    """Return True only for role-level junior signals, not mentoring language."""
    title_markers = (
        "junior ",
        "associate analyst",
        "entry level",
        "campus hire",
        "graduate program",
        "fresher",
    )
    if any(marker in title for marker in title_markers):
        return True

    body_patterns = (
        r"\bentry[\s-]level\b.{0,40}\b(role|position|opening|candidate)\b",
        r"\bcampus hire\b",
        r"\bgraduate program\b",
        r"\bfresher\b",
        r"\brecent graduates\b",
        r"\bassociate analyst\b.{0,40}\b(role|position|opening)\b",
        r"\bjunior\b.{0,20}\b(role|position|engineer|analyst|developer|associate)\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in body_patterns)


def _extract_experience_band(text: str) -> tuple[int, int, str, str] | None:
    """Extract explicit experience bands from job text when confidently present."""
    patterns = (
        r"\b(\d{1,2})\s*[-–to]+\s*(\d{1,2})\s+years?\b",
        r"\b(\d{1,2})\s*-\s*(\d{1,2})\s+yrs?\b",
        r"\bminimum\s+(\d{1,2})\s+to\s+(\d{1,2})\s+years?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            first = _safe_int(match.group(1))
            second = _safe_int(match.group(2))
            if first and second:
                return min(first, second), max(first, second), match.group(0), "range"

    plus_patterns = (
        r"\bminimum\s+(\d{1,2})\+?\s+years?\b",
        r"\bat least\s+(\d{1,2})\+?\s+years?\b",
        r"\b(\d{1,2})\+?\s+years?\s+of\s+experience\b",
    )
    for pattern in plus_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            years = _safe_int(match.group(1))
            if years:
                return years, years, match.group(0), "plus"
    return None


def _looks_like_heading(lowered: str) -> bool:
    heading_markers = (
        "responsibilities",
        "key responsibilities",
        "requirements",
        "qualifications",
        "basic qualifications",
        "preferred qualifications",
        "what you'll need",
        "what we are looking for",
        "what we're looking for",
        "role summary",
        "job description",
        "about the role",
        "must have",
        "nice to have",
    )
    return lowered in heading_markers or lowered.endswith(":")


def _is_requirement_like(lowered: str) -> bool:
    markers = (
        "requirements",
        "qualifications",
        "what you'll need",
        "what we are looking for",
        "what we're looking for",
        "must have",
        "skills",
        "experience",
        "responsibilities",
        "preferred qualifications",
        "basic qualifications",
    )
    return any(marker in lowered for marker in markers)


def _screen_line_score(line: str, lowered: str, active_section: str, index: int) -> int:
    score = 0

    if _is_boilerplate_line(lowered):
        return -100

    if active_section:
        if any(token in active_section for token in ("basic qualifications", "preferred qualifications", "requirements", "must have")):
            score += 6
        elif any(token in active_section for token in ("responsibilities", "role summary", "about the role", "job description")):
            score += 4
        elif "nice to have" in active_section:
            score += 1

    if _looks_like_heading(lowered):
        score += 5

    if line.startswith(("-", "*", "•", "â€¢")):
        score += 3

    high_signal_patterns = (
        r"\b\d+\+?\s+years?\b",
        r"\bexperience\b",
        r"\bqualification",
        r"\brequire",
        r"\bresponsib",
        r"\bmust have\b",
        r"\bskills?\b",
        r"\bknowledge\b",
        r"\bpython\b|\bjava\b|\bsql\b|\bai\b|\bml\b|\brag\b|\biam\b|\bsecurity\b|\barchitecture\b|\bproduct\b|\bdelivery\b",
    )
    for pattern in high_signal_patterns:
        if re.search(pattern, lowered):
            score += 2

    if len(line) > 280:
        score -= 3
    elif len(line) < 4:
        score -= 3

    if index < 3:
        score += 1

    return score


def _is_boilerplate_line(lowered: str) -> bool:
    boilerplate_patterns = (
        "world leader in payments technology",
        "join visa and do work that matters",
        "progress starts with you",
        "eeo employer",
        "qualified applicants will receive consideration",
        "without regard to race",
        "protected veteran status",
        "opportunity to create impact at scale",
        "join us and do work that matters",
        "company description",
    )
    return any(pattern in lowered for pattern in boilerplate_patterns)


def _parse_screen_response(content: str) -> dict:
    """Parse and normalize screener JSON."""
    cleaned = _extract_message_text(content).strip()
    if not cleaned:
        return _default_screen_result(
            reason="Screener parse failure: empty model response",
            decision="review",
            confidence=0.0,
            parse_error="Empty model response",
        )
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}")
    if json_start != -1 and json_end != -1 and json_end > json_start:
        cleaned = cleaned[json_start:json_end + 1]
    try:
        parsed = json.loads(cleaned)
        decision = _normalize_screen_decision(parsed.get("decision", "review"))
        confidence = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return {
            "decision": decision,
            "confidence": confidence,
            "role_family_fit": str(parsed.get("role_family_fit", "adjacent")).strip().lower() or "adjacent",
            "seniority_fit": str(parsed.get("seniority_fit", "unclear")).strip().lower() or "unclear",
            "location_fit": str(parsed.get("location_fit", "unknown")).strip().lower() or "unknown",
            "must_have_hits": _string_list(parsed.get("must_have_hits", [])),
            "must_have_misses": _string_list(parsed.get("must_have_misses", [])),
            "reason": str(parsed.get("reason", "")).strip(),
            "parse_error": "",
        }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _default_screen_result(
            reason=f"Screener parse failure: {exc}",
            decision="review",
            confidence=0.0,
            parse_error=f"{type(exc).__name__}: {exc}",
        )


def _default_screen_result(
    reason: str,
    decision: str = "review",
    confidence: float = 0.0,
    parse_error: str = "",
    model_used: str = "",
    raw_prompt: str = "",
    raw_content: str = "",
    raw_response_json: str = "",
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
        "raw_content": raw_content,
        "raw_response_json": raw_response_json,
        "screen_attempts": list(screen_attempts or []),
    }


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_repair_prompt(previous_response: str) -> str:
    compact = _extract_message_text(previous_response).strip()
    return SCREEN_REPAIR_PROMPT_TEMPLATE.format(previous_response=compact)


def should_bypass_premium_score(screen_result: dict) -> bool:
    """Return True when the screen is confident enough to skip premium scoring."""
    decision = _normalize_screen_decision(screen_result.get("decision", "review"))
    confidence = screen_result.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    return decision == "reject" and confidence >= SCREEN_REJECT_CONFIDENCE


def _normalize_screen_decision(value: object) -> str:
    decision = str(value or "review").strip().lower()
    if decision == "borderline":
        return "review"
    if decision not in {"pass", "review", "reject"}:
        return "review"
    return decision


def build_screen_reject_analysis(screen_result: dict, scoring_version: str) -> dict:
    """Build a stable zero-score analysis for confident screen rejects."""
    misses = _string_list(screen_result.get("must_have_misses", []))
    reason = str(screen_result.get("reason", "")).strip() or "Rejected by fast screener."
    role_family = str(screen_result.get("role_family_fit", "mismatch")).strip().lower() or "mismatch"
    seniority = str(screen_result.get("seniority_fit", "unclear")).strip().lower()
    location_fit = str(screen_result.get("location_fit", "unknown")).strip().lower() or "unknown"
    seniority_map = {
        "matched": "matched",
        "underleveled": "underleveled",
        "overleveled": "overleveled",
        "unclear": "matched",
    }
    return {
        "score": 0,
        "component_scores": {},
        "hard_requirements_met": _string_list(screen_result.get("must_have_hits", [])),
        "hard_requirements_missing": misses,
        "nice_to_have_matches": [],
        "role_family_match": role_family if role_family in {"strong", "adjacent", "mismatch"} else "mismatch",
        "seniority_fit": seniority_map.get(seniority, "matched"),
        "location_signal": location_fit if location_fit in {"matched", "relocation_required", "mismatch", "unknown"} else "unknown",
        "location_preference_applied": False,
        "parse_error": "",
        "strengths": [],
        "gaps": [reason] + misses[:2],
        "verdict": f"Screen rejected before premium scoring: {reason}",
        "thinking": "",
        "raw_system_prompt": "",
        "raw_user_prompt": "",
        "raw_llm_content": "",
        "raw_llm_reasoning": "",
        "raw_llm_response_json": "",
        "screen_result": screen_result,
        "screen_system_prompt": SCREEN_SYSTEM_PROMPT,
        "screen_user_prompt": screen_result.get("raw_prompt", ""),
        "screen_raw_output": screen_result.get("raw_content", ""),
        "screen_raw_response_json": screen_result.get("raw_response_json", ""),
        "screen_model_used": screen_result.get("model_used", ""),
        "screen_attempts": screen_result.get("screen_attempts", []),
        "qa_result": {},
        "qa_system_prompt": "",
        "qa_user_prompt": "",
        "qa_raw_output": "",
        "qa_raw_response_json": "",
        "qa_model_used": "",
        "prepared_job_description": "",
        "model_used": "",
        "scoring_version": scoring_version,
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _extract_message_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part).strip()
    return str(value)
