"""Candidate profile onboarding and sanitization utilities."""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "candidate_profile.json"

_DOMAIN_RULES = {
    "Core Banking": ("core banking", "finacle", "cbs"),
    "Payments": ("payments", "swift", "rtgs", "upi", "nach", "imps", "iso20022"),
    "Fintech": ("fintech", "api gateway", "digital banking"),
    "Delivery": ("delivery", "implementation", "rollout", "deployment", "program"),
    "Architecture": ("architecture", "solution design", "hld", "lld", "platform"),
    "Product / Platform": ("product", "platform", "roadmap", "stakeholder"),
    "Wealth Management": ("wealth management", "private banking"),
    "Investment Banking": ("investment banking",),
    "Operations / Support": ("operations", "uat", "sit", "support", "reconciliation"),
}

_EVIDENCE_RULES = {
    "solution_architecture": ("solution architecture", "solution design", "hld", "lld", "architect"),
    "platform_delivery": ("delivery", "implementation", "deployment", "rollout"),
    "core_banking": ("core banking", "finacle", "cbs"),
    "payments": ("swift", "rtgs", "upi", "nach", "imps", "payments"),
    "stakeholder_management": ("stakeholder", "liaise", "cross functional", "vendor"),
    "team_leadership": ("lead in house development team", "led in house development team", "resources", "manager"),
    "product_management_adjacent": ("new products", "business initiatives", "priorit", "stakeholder"),
    "ai_ml_product": ("machine learning", "artificial intelligence", "copilot", "agentic ai", "genai"),
    "fx_operations": ("fx option", "fx options", "fx forward", "fx forwards", "fx swaps", "foreign exchange"),
    "middle_office_controls": ("middle office", "product control", "pnl attribution", "profit and loss"),
    "institutional_investment_ops": ("institutional investment operations", "institutional trades", "isda"),
    "enterprise_architecture": ("togaf", "design authority", "architecture governance", "platform design authority"),
}

_NON_EVIDENCED_LABELS = {
    "fx_operations": "FX derivatives operations",
    "middle_office_controls": "Middle office / product control",
    "institutional_investment_ops": "Institutional investment operations",
    "ai_ml_product": "Deep AI/ML product ownership",
}


def build_or_load_candidate_profile(
    resume_text: str,
    candidate_preferences: dict,
    preferred_locations: list[str],
    resume_path: str,
    profile_path: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Load a cached candidate profile or regenerate it when inputs change."""
    path = Path(profile_path) if profile_path else PROFILE_PATH
    source_hash = _source_hash(resume_text, candidate_preferences, preferred_locations)

    if not force_refresh and path.exists():
        profile = load_candidate_profile(path)
        if profile.get("source_hash") == source_hash:
            return profile

    profile = generate_candidate_profile(
        resume_text,
        candidate_preferences,
        preferred_locations,
        resume_path,
        source_hash,
    )
    write_candidate_profile(profile, path)
    return profile


def load_candidate_profile(path: str | Path | None = None) -> dict:
    """Load a previously generated candidate profile from disk."""
    target = Path(path) if path else PROFILE_PATH
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


def candidate_preferences_from_profile(profile: dict) -> dict:
    """Project runtime candidate preferences from the curated candidate profile."""
    return {
        "target_roles": profile.get("target_roles", []) or [],
        "avoid_role_families": profile.get("avoid_roles", []) or [],
        "seniority_preference": profile.get("target_seniority", "") or "",
        "notes_for_matcher": profile.get("notes_for_matcher", "") or "",
        "primary_domains": profile.get("core_domains", []) or [],
    }


def write_candidate_profile(profile: dict, path: str | Path | None = None) -> None:
    """Persist a generated candidate profile."""
    target = Path(path) if path else PROFILE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)


def generate_candidate_profile(
    resume_text: str,
    candidate_preferences: dict,
    preferred_locations: list[str],
    resume_path: str,
    source_hash: str | None = None,
) -> dict:
    """Build a sanitized, LLM-friendly candidate profile from the raw resume."""
    redacted = redact_resume_text(resume_text)
    years = _extract_years_experience(redacted)
    domains = _extract_domains(redacted)
    evidence = _extract_evidence(redacted)
    non_evidenced = [
        label for key, label in _NON_EVIDENCED_LABELS.items() if not evidence.get(key, False)
    ]
    role_history = _extract_role_history(redacted)
    summary_short = _build_short_summary(years, domains, candidate_preferences)
    summary_long = _build_long_summary(summary_short, role_history, candidate_preferences)
    evidence_highlights = _build_evidence_highlights(evidence)
    profile = {
        "profile_version": "v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_resume_file": Path(resume_path).name,
        "source_hash": source_hash or _source_hash(resume_text, candidate_preferences, preferred_locations),
        "target_roles": candidate_preferences.get("target_roles", []),
        "avoid_roles": (
            candidate_preferences.get("exclude_role_families")
            or candidate_preferences.get("avoid_role_families")
            or []
        ),
        "target_seniority": candidate_preferences.get("seniority_preference", ""),
        "preferred_locations": preferred_locations,
        "core_domains": domains,
        "years_experience": years,
        "candidate_summary_short": summary_short,
        "candidate_summary_long": summary_long,
        "evidence_inventory": evidence,
        "evidence_highlights": evidence_highlights,
        "not_evidenced_directly": non_evidenced,
        "role_history": role_history,
        "resume_excerpt_redacted": _resume_excerpt(redacted),
        "notes_for_matcher": candidate_preferences.get("notes_for_matcher", ""),
    }
    if "Operations" in profile["avoid_roles"] and "Operations / Support" in profile["core_domains"]:
        profile["core_domains"].remove("Operations / Support")
    profile["llm_context"] = format_candidate_profile(profile)
    return profile


def redact_resume_text(resume_text: str) -> str:
    """Strip common personal identifiers from resume text before reuse/storage."""
    text = str(resume_text or "")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.\w+", "[email redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\+?\d[\d\s().-]{7,}\d)", "[phone redacted]", text)
    text = re.sub(r"https?://\S+|www\.\S+", "[link redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?im)^\s*(mobile|phone|email|linkedin|name|date of birth)\s*:.*$", "", text)
    text = re.sub(r"(?is)\bpersonal details\b.*$", "", text)
    lines = [line.strip() for line in text.splitlines()]
    if lines:
        first = lines[0]
        if 1 <= len(first.split()) <= 4 and ":" not in first and not any(ch.isdigit() for ch in first):
            lines = lines[1:]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_candidate_profile(profile: dict) -> str:
    """Render the richer candidate profile for premium scoring prompts."""
    lines = [
        f"Short summary: {profile.get('candidate_summary_short', '')}",
        f"Long summary: {profile.get('candidate_summary_long', '')}",
        f"Target roles: {', '.join(profile.get('target_roles', [])) or 'Not provided'}",
        f"Avoid roles: {', '.join(profile.get('avoid_roles', [])) or 'None'}",
        f"Preferred seniority: {profile.get('target_seniority', '') or 'Not provided'}",
        f"Preferred locations: {', '.join(profile.get('preferred_locations', [])) or 'Not provided'}",
        f"Core domains: {', '.join(profile.get('core_domains', [])) or 'Not provided'}",
        f"Evidence highlights: {', '.join(profile.get('evidence_highlights', [])) or 'None'}",
        f"Not directly evidenced: {', '.join(profile.get('not_evidenced_directly', [])) or 'None'}",
    ]
    notes = profile.get("notes_for_matcher", "")
    if notes:
        lines.append(f"Matcher notes: {notes}")
    excerpt = profile.get("resume_excerpt_redacted", "")
    if excerpt:
        lines.append("Full redacted resume context:")
        lines.append(excerpt)
    return "\n".join(lines)


def format_candidate_profile_for_screening(profile: dict) -> str:
    """Render a compact candidate profile for the fast triage stage."""
    lines = [
        f"Short summary: {profile.get('candidate_summary_short', '')}",
        f"Target roles: {', '.join(profile.get('target_roles', [])) or 'Not provided'}",
        f"Avoid roles: {', '.join(profile.get('avoid_roles', [])) or 'None'}",
        f"Preferred seniority: {profile.get('target_seniority', '') or 'Not provided'}",
        f"Preferred locations: {', '.join(profile.get('preferred_locations', [])) or 'Not provided'}",
        f"Core domains: {', '.join(profile.get('core_domains', [])) or 'Not provided'}",
        f"Evidence highlights: {', '.join(profile.get('evidence_highlights', [])) or 'None'}",
        f"Not directly evidenced: {', '.join(profile.get('not_evidenced_directly', [])) or 'None'}",
    ]
    notes = profile.get("notes_for_matcher", "")
    if notes:
        lines.append(f"Matcher notes: {notes}")
    return "\n".join(lines)


def _source_hash(resume_text: str, candidate_preferences: dict, preferred_locations: list[str]) -> str:
    raw = json.dumps(
        {
            "resume_text": resume_text,
            "candidate_preferences": candidate_preferences,
            "preferred_locations": preferred_locations,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _extract_years_experience(text: str) -> int:
    matches = [int(m) for m in re.findall(r"(\d{1,2})\+?\s+years", text, flags=re.IGNORECASE)]
    return max(matches) if matches else 0


def _extract_domains(text: str) -> list[str]:
    text_lower = text.lower()
    found = []
    for label, patterns in _DOMAIN_RULES.items():
        if any(pattern in text_lower for pattern in patterns):
            found.append(label)
    return found


def _extract_evidence(text: str) -> dict:
    text_lower = text.lower()
    return {
        key: any(pattern in text_lower for pattern in patterns)
        for key, patterns in _EVIDENCE_RULES.items()
    }


def _extract_role_history(text: str) -> list[str]:
    history = []
    for line in text.splitlines():
        cleaned = " ".join(line.split()).strip()
        if not cleaned:
            continue
        if re.search(
            r"\b(architect|manager|director|vice president|analyst|engineer|consultant)\b",
            cleaned,
            flags=re.IGNORECASE,
        ):
            if len(cleaned) <= 80 and "." not in cleaned and cleaned not in history:
                history.append(cleaned)
        if len(history) >= 6:
            break
    return history


def _build_short_summary(years: int, domains: list[str], candidate_preferences: dict) -> str:
    target_roles = candidate_preferences.get("target_roles", []) or []
    domain_text = ", ".join(domains[:3]) or "banking technology"
    role_text = ", ".join(target_roles[:3]) or "solution and delivery roles"
    years_text = f"{years}+ years" if years else "Experienced"
    return (
        f"{years_text} techno-functional leader focused on {domain_text}, "
        f"best aligned to {role_text}."
    )


def _build_long_summary(summary_short: str, role_history: list[str], candidate_preferences: dict) -> str:
    notes = candidate_preferences.get("notes_for_matcher", "")
    role_text = "; ".join(role_history[:4])
    parts = [summary_short]
    if role_text:
        parts.append(f"Recent role history includes {role_text}.")
    if notes:
        parts.append(f"Preference context: {notes}")
    return " ".join(parts)


def _build_evidence_highlights(evidence: dict) -> list[str]:
    highlights = []
    label_map = {
        "solution_architecture": "Solution architecture",
        "enterprise_architecture": "Architecture governance",
        "platform_delivery": "Platform delivery",
        "core_banking": "Core banking",
        "payments": "Payments",
        "stakeholder_management": "Stakeholder management",
        "team_leadership": "Team leadership",
        "product_management_adjacent": "Adjacent product ownership",
    }
    for key, label in label_map.items():
        if evidence.get(key):
            highlights.append(label)
    return highlights


def _resume_excerpt(text: str) -> str:
    return text.strip()
