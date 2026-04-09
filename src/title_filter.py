"""Title-based pre-filter — skip obviously irrelevant jobs before LLM scoring."""

import re
import logging

logger = logging.getLogger(__name__)

# Always skip these regardless of config (case-insensitive word-boundary match)
_BUILTIN_SKIP_PATTERNS = [
    r"\bintern\b",
    r"\binternship\b",
    r"\bco-op\b",
    r"\btrainee\b",
    r"\bapprentice\b",
    r"\bapprentices?hip\b",
]

_COMPILED_BUILTINS = [re.compile(p, re.IGNORECASE) for p in _BUILTIN_SKIP_PATTERNS]


def should_skip_job(title: str, candidate_preferences: dict) -> tuple[bool, str]:
    """Decide whether a job should be skipped based on its title.

    Returns:
        (should_skip, reason) — reason is empty string if not skipped.
    """
    if not title:
        return False, ""

    title_lower = title.lower()

    # 1. Built-in patterns (always skip)
    for pattern in _COMPILED_BUILTINS:
        if pattern.search(title):
            return True, f"built-in exclusion ({pattern.pattern})"

    # 2. User-configured exclusions (hard filter)
    exclude_families = (
        candidate_preferences.get("exclude_role_families")
        or candidate_preferences.get("avoid_role_families")
        or []
    )
    for family in exclude_families:
        family_lower = family.strip().lower()
        if not family_lower:
            continue
        # Match the family name as a substring in the title
        if family_lower in title_lower:
            return True, f"excluded role family: {family}"

    return False, ""


def make_skip_analysis(reason: str) -> dict:
    """Build a zero-score analysis dict for a title-filtered job."""
    return {
        "score": 0,
        "component_scores": {},
        "hard_requirements_met": [],
        "hard_requirements_missing": [],
        "nice_to_have_matches": [],
        "role_family_match": "mismatch",
        "seniority_fit": "matched",
        "location_signal": "unknown",
        "location_preference_applied": False,
        "parse_error": "",
        "strengths": [],
        "gaps": [f"Title filter: {reason}"],
        "verdict": f"Skipped by title filter: {reason}",
        "thinking": "",
        "raw_system_prompt": "",
        "raw_user_prompt": "",
        "raw_llm_content": "",
        "raw_llm_reasoning": "",
        "prepared_job_description": "",
        "model_used": "",
        "scoring_version": "",
    }
