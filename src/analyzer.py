"""LLM-based job-resume analyzer -- scores job fit via OpenRouter API."""

import json
import logging
import re

import requests

from src.candidate_profile import format_candidate_profile

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SCORING_VERSION = "v8"

SYSTEM_PROMPT = """You are a strict senior recruiter evaluating how well a candidate matches a specific job.

Your job is not to produce a vague recruiter-style summary. You must identify hard requirements, functional fit, and likely blockers, then return structured evidence.

Scoring rules:
1. Distinguish direct fit from adjacent fit.
2. Penalize missing hard requirements more than missing nice-to-have requirements.
3. Penalize functional mismatch strongly. Example: engineering leader vs customer-facing TAM role.
4. Penalize clear location mismatch only when the job appears location-bound.
5. Seniority mismatch matters, but it should not dominate unless the gap is severe.

Return ONLY valid JSON with exactly this shape:
{
  "component_scores": {
    "technical_skills": <0-100>,
    "role_alignment": <0-100>,
    "experience_level": <0-100>,
    "domain_knowledge": <0-100>,
    "education": <0-100>,
    "location_fit": <0-100>
  },
  "hard_requirements_met": ["item1", "item2"],
  "hard_requirements_missing": ["item1", "item2"],
  "nice_to_have_matches": ["item1", "item2"],
  "role_family_match": "strong|adjacent|mismatch",
  "seniority_fit": "underleveled|matched|overleveled",
  "location_signal": "matched|relocation_required|mismatch|unknown",
  "strengths": ["strength1", "strength2", "strength3"],
  "gaps": ["gap1", "gap2", "gap3"],
  "verdict": "one short paragraph"
}

Do not include markdown fences or any text outside the JSON."""

WEIGHTS = {
    "technical_skills": 0.30,
    "role_alignment": 0.30,
    "experience_level": 0.15,
    "domain_knowledge": 0.15,
    "education": 0.00,
    "location_fit": 0.10,
}

SEVERE_BLOCKER_PATTERNS = (
    "customer-facing",
    "customer facing",
    "saas",
    "account management",
    "key account",
    "merchant engagement",
    "merchant-facing",
    "merchant facing",
    "government officials",
    "alliances",
    "partnerships",
    "sales",
)

ROLE_HINT_MARKERS = (
    "required qualifications",
    "minimum qualifications",
    "basic qualifications",
    "job expectations",
    "must have",
    "required skills",
)

OFF_RADAR_TITLE_PATTERNS = (
    "operations",
    "middle office",
    "back office",
    "trade support",
    "settlement",
    "reconciliation",
    "analyst",
    "associate",
    "specialist",
)

TARGET_ROLE_HINTS = (
    "architect",
    "architecture",
    "solution",
    "delivery",
    "product",
    "platform",
    "program",
)

OPERATIONS_ROLE_MARKERS = (
    "middle office",
    "back office",
    "operations associate",
    "operations analyst",
    "trade support",
    "product control",
    "settlement",
    "reconciliation",
    "custodial operations",
    "securities settlement",
)

EVIDENCE_RULES = (
    {
        "label": "Direct FX derivatives operations experience",
        "job_patterns": ("fx options", "fx forwards", "fx swaps"),
        "resume_patterns": (
            "fx option",
            "fx options",
            "fx forward",
            "fx forwards",
            "fx swap",
            "fx swaps",
            "foreign exchange",
        ),
    },
    {
        "label": "Middle office or product control experience",
        "job_patterns": ("middle office", "product control", "trade & sales support"),
        "resume_patterns": (
            "middle office",
            "product control",
            "trade support",
            "trader assistant",
            "sales assistant",
        ),
    },
    {
        "label": "PnL / attribution reporting experience",
        "job_patterns": (
            "pnl attribution",
            "profit and loss",
            "daily risk & pnl",
            "unrealized pnl",
            "realized pnl",
        ),
        "resume_patterns": (
            "pnl",
            "p&l",
            "profit and loss",
            "attribution",
            "product control",
        ),
    },
    {
        "label": "Institutional investment operations background",
        "job_patterns": (
            "institutional investment operations",
            "institutional trades",
            "securities settlement",
            "asset servicing",
            "custodial operations",
            "isda",
        ),
        "resume_patterns": (
            "institutional investment operations",
            "institutional trades",
            "securities settlement",
            "asset servicing",
            "custodial operations",
            "isda",
        ),
    },
)

REPAIR_PROMPT_TEMPLATE = """Your previous response was not valid JSON for the required schema.

Return ONLY valid JSON that matches the exact schema from the system instructions.
Do not include markdown fences, explanation, or any text outside the JSON.

Previous invalid response:
{previous_response}"""


def score_job(
    resume_text: str,
    job: dict,
    model: str,
    api_key: str,
    preferred_locations: list[str] | None = None,
    candidate_preferences: dict | None = None,
    candidate_profile: dict | None = None,
    max_retries: int = 3,
) -> dict:
    """Score how well a resume matches a job posting using an LLM."""
    description = job.get("description", "No description available.")
    prepared_description = _prepare_description(description)

    preferred_locations = preferred_locations or []
    candidate_preferences = candidate_preferences or {}
    preferred_locations_text = (
        ", ".join(preferred_locations)
        if preferred_locations
        else "No preferred locations provided."
    )
    candidate_preferences_text = _format_candidate_preferences(candidate_preferences)
    candidate_context = _build_candidate_context(candidate_profile, resume_text, candidate_preferences)
    user_prompt = _build_user_prompt(
        candidate_context,
        job,
        preferred_locations_text,
        candidate_preferences_text,
        prepared_description,
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jobhunter-agent",
        "X-Title": "Job Hunter Agent",
    }

    last_error = None
    current_user_prompt = user_prompt

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                f"  Scoring: {job.get('title', '?')} @ {job.get('company', '?')}"
                + (f" (attempt {attempt})" if attempt > 1 else "")
            )

            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = _extract_message_text(message.get("content"))
            reasoning = _extract_message_text(
                message.get("reasoning", "")
                or message.get("reasoning_content", "")
            )

            result = _parse_llm_response(content)
            if result.get("parse_error"):
                result["thinking"] = reasoning
                result["raw_system_prompt"] = SYSTEM_PROMPT
                result["raw_user_prompt"] = current_user_prompt
                result["raw_llm_content"] = content
                result["raw_llm_reasoning"] = reasoning
                result["raw_llm_response_json"] = json.dumps(data, ensure_ascii=False)
                result["prepared_job_description"] = prepared_description
                result["model_used"] = model
                result["scoring_version"] = SCORING_VERSION
                if attempt < max_retries:
                    logger.warning(
                        "  Parse failure on attempt %s/%s; retrying with JSON repair prompt",
                        attempt,
                        max_retries,
                    )
                    current_user_prompt = _build_repair_prompt(content)
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": current_user_prompt},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 4096,
                    }
                    continue
                return result

            result = _apply_guardrails(
                result,
                job,
                candidate_preferences,
                prepared_description,
                resume_text,
            )
            result = _apply_location_preferences(result, job, preferred_locations)
            result["thinking"] = reasoning
            result["raw_system_prompt"] = SYSTEM_PROMPT
            result["raw_user_prompt"] = current_user_prompt
            result["raw_llm_content"] = content
            result["raw_llm_reasoning"] = reasoning
            result["raw_llm_response_json"] = json.dumps(data, ensure_ascii=False)
            result["prepared_job_description"] = prepared_description
            result["model_used"] = model
            result["scoring_version"] = SCORING_VERSION
            return result

        except requests.RequestException as exc:
            last_error = exc
            status_code = (
                getattr(exc.response, "status_code", None)
                if hasattr(exc, "response")
                else None
            )
            logger.warning(f"  Attempt {attempt}/{max_retries} failed: {exc}")

            if status_code in (429, 500, 502, 503) and attempt < max_retries:
                wait = 2 ** attempt
                logger.info(f"  Retrying in {wait}s...")
                import time

                time.sleep(wait)
                continue

            break

    logger.error(f"  OpenRouter API error after {max_retries} attempts: {last_error}")
    return {
        "score": 0,
        "component_scores": {},
        "hard_requirements_met": [],
        "hard_requirements_missing": ["API call failed"],
        "nice_to_have_matches": [],
        "role_family_match": "mismatch",
        "seniority_fit": "matched",
        "location_signal": "unknown",
        "location_preference_applied": False,
        "parse_error": f"API call failed: {last_error}",
        "strengths": [],
        "gaps": ["API call failed"],
        "verdict": f"Error: {str(last_error)}",
        "thinking": "",
        "raw_system_prompt": SYSTEM_PROMPT,
        "raw_user_prompt": current_user_prompt,
        "raw_llm_content": "",
        "raw_llm_reasoning": "",
        "raw_llm_response_json": "",
        "prepared_job_description": prepared_description,
        "model_used": model,
        "scoring_version": SCORING_VERSION,
    }


_REQ_MARKERS = [
    "requirements", "qualifications", "what you'll need",
    "what we're looking for", "must have", "skills",
    "who you are", "what you bring", "minimum qualifications",
    "basic qualifications", "preferred qualifications",
]


def _prepare_description(description: str) -> str:
    """Return the full job description without truncation."""
    return description


def _build_repair_prompt(previous_response: str) -> str:
    """Ask the model to re-emit the prior answer as valid JSON only."""
    compact = _extract_message_text(previous_response).strip()
    return REPAIR_PROMPT_TEMPLATE.format(previous_response=compact)


def _build_candidate_context(
    candidate_profile: dict | None,
    resume_text: str,
    candidate_preferences: dict,
) -> str:
    """Prefer a sanitized candidate profile over raw resume text in prompts."""
    if candidate_profile:
        return format_candidate_profile(candidate_profile)
    return (
        "Short summary unavailable. Fall back to this redacted resume excerpt:\n"
        + _fallback_resume_excerpt(resume_text)
        + "\n\n"
        + _format_candidate_preferences(candidate_preferences)
    )


def _build_user_prompt(
    candidate_context: str,
    job: dict,
    preferred_locations_text: str,
    candidate_preferences_text: str,
    prepared_description: str,
) -> str:
    """Build the LLM user prompt from sanitized candidate context plus job data."""
    return f"""## Candidate Profile
{candidate_context}

---

## Job Posting
Title: {job.get('title', 'N/A')}
Location: {job.get('location', 'N/A')}
Company: {job.get('company', 'N/A')}
Configured Preferred Locations: {preferred_locations_text}

## Candidate Preferences
{candidate_preferences_text}

---

Description:
{prepared_description}

---

Analyze the fit using the required JSON schema. Be explicit about functional-role mismatch, missing hard requirements, and overqualification when relevant.

Important location rule:
- If the job location matches one of the Configured Preferred Locations, treat location as matched.
- Do not penalize or mention relocation for jobs that fall within the configured preferred locations.
- Only use relocation_required or mismatch when the job is clearly outside the configured preferred locations.

Important preference rule:
- Treat Candidate Preferences as directional guidance, not absolute blockers.
- Use them to break ties and shape borderline judgments.
- Do not override explicit hard requirements in the job posting based only on Candidate Preferences.

Important evidence rule:
- Do not infer direct experience unless the Candidate Profile explicitly supports it.
- Treat items listed as "Not directly evidenced" as missing unless the job only requires adjacent exposure."""


def _format_candidate_preferences(candidate_preferences: dict) -> str:
    """Render candidate preferences from config into prompt-friendly text."""
    if not candidate_preferences:
        return "No candidate preferences provided."

    lines = []
    for label, key in (
        ("Target roles", "target_roles"),
        ("Avoid role families", "avoid_role_families"),
        ("Primary domains", "primary_domains"),
    ):
        values = candidate_preferences.get(key, [])
        if values:
            lines.append(f"{label}: {', '.join(str(v) for v in values)}")

    seniority = candidate_preferences.get("seniority_preference", "")
    if seniority:
        lines.append(f"Preferred seniority: {seniority}")

    notes = candidate_preferences.get("notes_for_matcher", "")
    if notes:
        lines.append(f"Matcher notes: {notes}")

    return "\n".join(lines) if lines else "No candidate preferences provided."


def _fallback_resume_excerpt(resume_text: str) -> str:
    """Redact common PII from a raw resume when no candidate profile is available."""
    text = str(resume_text or "")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.\w+", "[email redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\+?\d[\d\s().-]{7,}\d)", "[phone redacted]", text)
    text = re.sub(r"https?://\S+|www\.\S+", "[link redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_location(value: str) -> str:
    """Normalize location text for loose matching."""
    normalized = str(value or "").strip().lower()
    replacements = {
        "bengaluru": "bangalore",
        "bangaluru": "bangalore",
    }
    for src, dest in replacements.items():
        normalized = normalized.replace(src, dest)
    return normalized


def _job_matches_preferred_locations(job: dict, preferred_locations: list[str] | None) -> bool:
    """Return True when the job location is covered by configured preferred locations."""
    if not preferred_locations:
        return False

    job_location = _normalize_location(job.get("location", ""))
    if not job_location:
        return False

    prefs = [_normalize_location(pref) for pref in preferred_locations if str(pref).strip()]
    return any(pref in job_location or job_location in pref for pref in prefs)


def _apply_location_preferences(parsed: dict, job: dict, preferred_locations: list[str] | None) -> dict:
    """Override location penalty when the job is already in an approved preferred location."""
    if not _job_matches_preferred_locations(job, preferred_locations):
        parsed["location_preference_applied"] = False
        parsed["score"] = _compute_final_score(parsed)
        return parsed

    parsed["location_signal"] = "matched"
    parsed["location_preference_applied"] = True
    verdict = parsed.get("verdict", "")
    if verdict:
        verdict = re.sub(
            r"(?i)\b(?:and|but)?\s*would need to relocate from [^.]+?\.",
            ".",
            verdict,
        )
        verdict = re.sub(
            r"(?i)\b(?:and|but)?\s*relocation to [^.]+?\.",
            ".",
            verdict,
        )
        verdict = re.sub(r"(?i)\b(?:and|but)?\s*not currently located in [^.]+?\.", ".", verdict)
        verdict = re.sub(r"\s{2,}", " ", verdict)
        verdict = re.sub(r"\s+,", ",", verdict)
        verdict = re.sub(r",\s*\.", ".", verdict)
        verdict = verdict.replace("..", ".").strip(" ,")
        parsed["verdict"] = verdict

    gap_phrases = (
        "relocation",
        "location mismatch",
        "would need to relocate",
        "not currently located",
    )
    parsed["gaps"] = [
        gap for gap in parsed.get("gaps", [])
        if not any(phrase in str(gap).lower() for phrase in gap_phrases)
    ]
    parsed["score"] = _compute_final_score(parsed)
    return parsed


def _compute_final_score(parsed: dict) -> int:
    """Convert structured model output into a deterministic final score."""
    components = parsed.get("component_scores", {})
    weighted_total = 0.0
    for key, weight in WEIGHTS.items():
        weighted_total += weight * _clamp_score(components.get(key, 0))

    score = round(weighted_total)

    missing_items = parsed.get("hard_requirements_missing", [])
    score -= min(12, len(missing_items) * 6)
    score -= _severe_blocker_penalty(missing_items)

    role_family = parsed.get("role_family_match", "adjacent")
    if role_family == "mismatch":
        score -= 12
    elif role_family == "adjacent":
        role_alignment = _clamp_score(components.get("role_alignment", 0))
        if role_alignment < 60:
            score -= 4
        elif role_alignment < 75:
            score -= 2

    seniority_fit = parsed.get("seniority_fit", "matched")
    if seniority_fit == "underleveled":
        score -= 10
    elif seniority_fit == "overleveled":
        score -= 2

    location_signal = parsed.get("location_signal", "unknown")
    if location_signal == "mismatch":
        score -= 12
    elif location_signal == "relocation_required":
        score -= 5

    score -= max(0, int(parsed.get("guardrail_penalty", 0) or 0))

    # Golden match bonus
    if (role_family == "strong" and seniority_fit == "matched" 
        and location_signal in ("matched", "unknown")):
        score += 6

    return max(0, min(100, score))


def _clamp_score(value: object) -> int:
    """Normalize any component score into the 0-100 range."""
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 0
    return max(0, min(100, score))


def _extract_message_text(value: object) -> str:
    """Normalize OpenRouter message content/reasoning into plain text."""
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


def _severe_blocker_penalty(missing_items: list[str]) -> int:
    """Apply extra penalty for hard misses that imply core functional mismatch."""
    severe_hits = 0
    for item in missing_items:
        normalized = str(item).lower()
        if any(pattern in normalized for pattern in SEVERE_BLOCKER_PATTERNS):
            severe_hits += 1
    return min(16, severe_hits * 8)


def _apply_guardrails(
    parsed: dict,
    job: dict,
    candidate_preferences: dict,
    prepared_description: str,
    resume_text: str,
) -> dict:
    """Apply deterministic checks for obvious scoring drift before finalizing."""
    penalties = 0
    gap_notes: list[str] = []

    if _missing_requirement_extraction(parsed, prepared_description):
        penalties += 8
        gap_notes.append("Hard requirements were not identified from a detailed posting")
        if parsed.get("role_family_match") == "strong":
            parsed["role_family_match"] = "adjacent"

    role_guardrail = _deterministic_role_family_guardrail(
        job.get("title", ""),
        prepared_description,
        candidate_preferences,
    )
    penalties += role_guardrail["penalty"]
    gap_notes.extend(role_guardrail["notes"])
    _downgrade_role_family(parsed, role_guardrail["role_family"])

    evidence_missing = _missing_resume_evidence(prepared_description, resume_text)
    if evidence_missing:
        gap_notes.extend(
            f"Resume does not show clear evidence for: {item.lower()}"
            for item in evidence_missing
        )
        existing_missing = [str(item) for item in parsed.get("hard_requirements_missing", [])]
        for item in evidence_missing:
            if item not in existing_missing:
                existing_missing.append(item)
        parsed["hard_requirements_missing"] = existing_missing[:6]
        penalties += min(18, len(evidence_missing) * 6)
        _downgrade_role_family(parsed, "mismatch")

    _cap_component_scores(parsed)

    if gap_notes:
        existing_gaps = [str(gap) for gap in parsed.get("gaps", [])]
        for note in gap_notes:
            if note not in existing_gaps:
                existing_gaps.append(note)
        parsed["gaps"] = existing_gaps

    parsed["guardrail_penalty"] = min(20, penalties)
    parsed["score"] = _compute_final_score(parsed)
    return parsed


def _downgrade_role_family(parsed: dict, target: str) -> None:
    """Only move role family toward stricter outcomes."""
    current = parsed.get("role_family_match", "adjacent")
    order = {"strong": 0, "adjacent": 1, "mismatch": 2}
    if order.get(target, 1) > order.get(current, 1):
        parsed["role_family_match"] = target


def _cap_component_scores(parsed: dict) -> None:
    """Prevent mismatch cases from retaining inflated LLM component scores."""
    components = parsed.get("component_scores", {})
    role_family = parsed.get("role_family_match", "adjacent")
    missing_count = len(parsed.get("hard_requirements_missing", []))

    if role_family == "mismatch":
        components["role_alignment"] = min(_clamp_score(components.get("role_alignment", 0)), 55)
        components["domain_knowledge"] = min(_clamp_score(components.get("domain_knowledge", 0)), 60)

    if missing_count >= 2:
        components["technical_skills"] = min(_clamp_score(components.get("technical_skills", 0)), 70)


def _missing_requirement_extraction(parsed: dict, prepared_description: str) -> bool:
    """Flag detailed postings where the model failed to identify any requirements."""
    if parsed.get("hard_requirements_met") or parsed.get("hard_requirements_missing"):
        return False
    lower = prepared_description.lower()
    return any(marker in lower for marker in ROLE_HINT_MARKERS)


def _deterministic_role_family_guardrail(
    title: str,
    prepared_description: str,
    candidate_preferences: dict,
) -> dict:
    """Apply title and JD-based role-family checks before trusting the model."""
    target_roles = candidate_preferences.get("target_roles", []) or []
    if not target_roles:
        return {"penalty": 0, "notes": [], "role_family": "strong"}

    title_lower = str(title or "").lower()
    target_text = " ".join(str(role).lower() for role in target_roles)
    desc_lower = prepared_description.lower()

    strategic_targets = any(hint in target_text for hint in TARGET_ROLE_HINTS)
    title_matches_target = any(hint in title_lower for hint in TARGET_ROLE_HINTS)
    title_is_off_radar = any(pattern in title_lower for pattern in OFF_RADAR_TITLE_PATTERNS)
    operations_signal_count = sum(
        1 for pattern in OPERATIONS_ROLE_MARKERS if pattern in title_lower or pattern in desc_lower
    )

    penalty = 0
    notes: list[str] = []
    role_family = "strong"

    if strategic_targets and title_is_off_radar and not title_matches_target:
        penalty += 12
        notes.append("Title skews toward operations/support rather than target role families")
        role_family = "adjacent"

        if operations_signal_count >= 3:
            penalty += 6
            notes.append("Job description is dominated by operational middle-office signals")
            role_family = "mismatch"

    seniority_pref = str(candidate_preferences.get("seniority_preference", "")).lower()
    if seniority_pref == "manager_to_senior_manager" and any(
        marker in title_lower for marker in ("analyst", "associate", "specialist")
    ):
        penalty += 6
        notes.append("Title appears below the preferred seniority band")
        if role_family == "strong":
            role_family = "adjacent"

    return {"penalty": min(18, penalty), "notes": notes, "role_family": role_family}


def _missing_resume_evidence(prepared_description: str, resume_text: str) -> list[str]:
    """Identify job-critical requirements that are absent from the resume text."""
    desc_lower = prepared_description.lower()
    resume_lower = str(resume_text or "").lower()
    missing: list[str] = []

    for rule in EVIDENCE_RULES:
        if not any(pattern in desc_lower for pattern in rule["job_patterns"]):
            continue
        if any(pattern in resume_lower for pattern in rule["resume_patterns"]):
            continue
        missing.append(rule["label"])

    return missing[:3]


def _parse_llm_response(content: str) -> dict:
    """Parse and normalize the structured JSON response from the LLM."""
    cleaned = _extract_message_text(content).strip()
    if not cleaned:
        logger.warning("  Failed to parse LLM response as structured JSON: empty content")
        return {
            "score": 0,
            "component_scores": {},
            "hard_requirements_met": [],
            "hard_requirements_missing": ["Model returned empty content"],
            "nice_to_have_matches": [],
            "role_family_match": "mismatch",
            "seniority_fit": "matched",
            "location_signal": "unknown",
            "location_preference_applied": False,
            "parse_error": "Empty model response",
            "strengths": [],
            "gaps": ["Model returned empty content"],
            "verdict": "Parse failure: model returned empty content.",
        }
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        component_scores = {
            key: _clamp_score(parsed.get("component_scores", {}).get(key, 0))
            for key in WEIGHTS
        }
        normalized = {
            "component_scores": component_scores,
            "hard_requirements_met": parsed.get("hard_requirements_met", []),
            "hard_requirements_missing": parsed.get("hard_requirements_missing", []),
            "nice_to_have_matches": parsed.get("nice_to_have_matches", []),
            "role_family_match": parsed.get("role_family_match", "adjacent"),
            "seniority_fit": parsed.get("seniority_fit", "matched"),
            "location_signal": parsed.get("location_signal", "unknown"),
            "location_preference_applied": False,
            "parse_error": "",
            "strengths": parsed.get("strengths", []),
            "gaps": parsed.get("gaps", []),
            "verdict": parsed.get("verdict", ""),
            "guardrail_penalty": 0,
        }
        normalized["score"] = _compute_final_score(normalized)
        return normalized
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(f"  Failed to parse LLM response as structured JSON: {exc}")
        return {
            "score": 0,
            "component_scores": {},
            "hard_requirements_met": [],
            "hard_requirements_missing": ["Could not parse structured response"],
            "nice_to_have_matches": [],
            "role_family_match": "mismatch",
            "seniority_fit": "matched",
            "location_signal": "unknown",
            "location_preference_applied": False,
            "parse_error": f"{type(exc).__name__}: {exc}",
            "strengths": [],
            "gaps": ["Could not parse structured response"],
            "verdict": "Parse failure: model did not return valid structured JSON.",
        }
