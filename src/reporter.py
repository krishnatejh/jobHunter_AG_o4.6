"""Report generator — renders HTML report from scored job results."""

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from src.analyzer import WEIGHTS

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _score_class(score: int) -> str:
    """Map a numeric score to a CSS class."""
    if score >= 85:
        return "score-excellent"
    if score >= 70:
        return "score-good"
    if score >= 50:
        return "score-moderate"
    return "score-weak"


def _score_label(score: int) -> str:
    """Human-readable label for a score."""
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Moderate"
    if score > 0:
        return "Weak"
    return "Error"


ROLE_FAMILY_LABELS = {
    "strong": "",
    "adjacent": "Adjacent role match",
    "mismatch": "Role mismatch",
}

SENIORITY_LABELS = {
    "matched": "",
    "underleveled": "Underleveled",
    "overleveled": "Overleveled",
}

LOCATION_LABELS = {
    "matched": "",
    "relocation_required": "Relocation needed",
    "mismatch": "Location mismatch",
    "unknown": "",
}

ROLE_FACT_LABELS = {
    "strong": "Direct fit",
    "adjacent": "Adjacent fit",
    "mismatch": "Role mismatch",
}

SENIORITY_FACT_LABELS = {
    "underleveled": "Underleveled",
    "matched": "Matched",
    "overleveled": "Overleveled",
}

LOCATION_FACT_LABELS = {
    "matched": "Preferred location match",
    "relocation_required": "Relocation needed",
    "mismatch": "Outside preferred locations",
    "unknown": "Location unclear",
}

SCREEN_LABELS = {
    "pass": "Pass",
    "borderline": "Review",
    "review": "Review",
    "reject": "Reject",
}


def _compact_reason(text: str) -> str:
    """Shorten a reason so it fits comfortably in table cells."""
    if not text:
        return ""
    trimmed = " ".join(str(text).strip().split())
    if len(trimmed) <= 60:
        return trimmed
    return trimmed[:57].rstrip(" ,;:.") + "..."


def _waitlist_reason(job: dict) -> str:
    """Create a crisp explanation for why a low-score job landed in Waitlist."""
    analysis = job.get("analysis", {})
    reasons = []

    missing = analysis.get("hard_requirements_missing") or []
    if missing:
        reasons.append(_compact_reason(missing[0]))

    role_reason = ROLE_FAMILY_LABELS.get(analysis.get("role_family_match", ""), "")
    if role_reason:
        reasons.append(role_reason)

    seniority_reason = SENIORITY_LABELS.get(analysis.get("seniority_fit", ""), "")
    if seniority_reason:
        reasons.append(seniority_reason)

    location_reason = LOCATION_LABELS.get(analysis.get("location_signal", ""), "")
    if location_reason:
        reasons.append(location_reason)

    gaps = analysis.get("gaps") or []
    for gap in gaps:
        compact = _compact_reason(gap)
        if compact and compact not in reasons:
            reasons.append(compact)
        if len(reasons) >= 3:
            break

    if not reasons:
        return "Low overall fit"

    return " | ".join(reasons[:3])


def _analysis_facts(job: dict) -> list[str]:
    """Create compact scoring facts for analysis cards."""
    analysis = job.get("analysis", {})
    facts = []

    role = ROLE_FACT_LABELS.get(analysis.get("role_family_match", ""), "")
    if role:
        facts.append(f"Role: {role}")

    seniority = SENIORITY_FACT_LABELS.get(analysis.get("seniority_fit", ""), "")
    if seniority:
        facts.append(f"Seniority: {seniority}")

    location = LOCATION_FACT_LABELS.get(analysis.get("location_signal", ""), "")
    if location:
        if analysis.get("location_preference_applied"):
            location = "Preferred location honored"
        facts.append(f"Location: {location}")

    for missing in (analysis.get("hard_requirements_missing") or [])[:2]:
        compact = _compact_reason(missing)
        if compact:
            facts.append(f"Missing: {compact}")

    return facts[:5]


def _llm_base_score(analysis: dict) -> int:
    """Reconstruct the base LLM-derived score before deterministic adjustments."""
    components = analysis.get("component_scores", {}) or {}
    weighted_total = 0.0
    for key, weight in WEIGHTS.items():
        try:
            component = int(components.get(key, 0))
        except (TypeError, ValueError):
            component = 0
        component = max(0, min(100, component))
        weighted_total += weight * component
    return max(0, min(100, round(weighted_total)))


def _score_adjustment(analysis: dict) -> int:
    """Difference between final score and the base LLM-derived score."""
    return analysis.get("score", 0) - _llm_base_score(analysis)


def _format_adjustment(value: int) -> str:
    """Format a score adjustment with an explicit sign."""
    if value > 0:
        return f"+{value}"
    return str(value)


def _screen_display(job: dict) -> tuple[str, str]:
    """Return a compact screen-stage label and CSS class."""
    screen = job.get("analysis", {}).get("screen_result", {}) or {}
    decision = str(screen.get("decision", "")).strip().lower()
    if decision not in SCREEN_LABELS:
        return "N/A", "screen-na"
    css_key = "review" if decision == "borderline" else decision
    return SCREEN_LABELS[decision], f"screen-{css_key}"


def prepare_results(results: list[dict]) -> list[dict]:
    """Sort results and attach display fields used by reports and automation."""
    results.sort(key=lambda j: j.get("analysis", {}).get("score", 0), reverse=True)

    for i, job in enumerate(results):
        analysis = job.get("analysis", {})
        score = analysis.get("score", 0)
        job["score_class"] = _score_class(score)
        job["score_label"] = _score_label(score)
        job["anchor_id"] = f"job-{i}"
        job["cache_status"] = job.get("cache_status", "unknown")
        job["status_label"] = {
            "new": "New",
            "cached": "Cached",
            "reposted": "Reposted",
            "filtered": "Filtered",
        }.get(job["cache_status"], "Unknown")
        analysis["llm_base_score"] = _llm_base_score(analysis)
        analysis["score_adjustment"] = _score_adjustment(analysis)
        analysis["score_adjustment_label"] = _format_adjustment(
            analysis["score_adjustment"]
        )
        job["waitlist_reason"] = _waitlist_reason(job)
        job["analysis_facts"] = _analysis_facts(job)
        job["screen_label"], job["screen_class"] = _screen_display(job)

    return results


def bucket_results(results: list[dict]) -> dict[str, list[dict]]:
    """Split results into the four report buckets."""
    def is_priority(job: dict) -> bool:
        return job.get("analysis", {}).get("score", 0) >= 70

    return {
        "new_findings": [
            job for job in results
            if job.get("cache_status") in {"new", "reposted"} and is_priority(job)
        ],
        "cached_findings": [
            job for job in results
            if job.get("cache_status") == "cached" and is_priority(job)
        ],
        "borderline_jobs": [
            job for job in results
            if 50 <= job.get("analysis", {}).get("score", 0) < 70
        ],
        "waitlist_jobs": [
            job for job in results
            if job.get("analysis", {}).get("score", 0) < 50
        ],
    }


def summarize_results(results: list[dict]) -> dict:
    """Create a compact summary payload for automation and notifications."""
    buckets = bucket_results(results)
    top_matches = []
    for job in results[:5]:
        top_matches.append({
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "score": job.get("analysis", {}).get("score", 0),
            "location": job.get("location", ""),
            "cache_status": job.get("cache_status", "unknown"),
            "url": job.get("url", ""),
        })

    return {
        "sections": {
            "new_findings": len(buckets["new_findings"]),
            "cached_findings": len(buckets["cached_findings"]),
            "borderline": len(buckets["borderline_jobs"]),
            "waitlist": len(buckets["waitlist_jobs"]),
        },
        "ratings": {
            "excellent": sum(1 for j in results if j.get("analysis", {}).get("score", 0) >= 85),
            "good": sum(1 for j in results if 70 <= j.get("analysis", {}).get("score", 0) < 85),
            "moderate": sum(1 for j in results if 50 <= j.get("analysis", {}).get("score", 0) < 70),
            "weak_or_error": sum(1 for j in results if j.get("analysis", {}).get("score", 0) < 50),
        },
        "top_matches": top_matches,
    }


def generate_report(
    results: list[dict],
    resume_name: str,
    model: str,
    filter_stats: dict | None = None,
    preferred_locations: list[str] | None = None,
) -> str:
    """Generate an HTML report from scored job results.

    Args:
        results: List of job dicts, each containing an 'analysis' sub-dict.
        resume_name: Resume/profile label used for display.
        model: LLM model used for scoring.
        filter_stats: Dict with total_scanned, skipped_recency, skipped_location, matched.

    Returns:
        Path to the generated HTML file.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Group by company
    companies = defaultdict(list)
    for job in results:
        companies[job.get("company", "Unknown")].append(job)
    buckets = bucket_results(results)

    filter_options = {
        "companies": sorted({job.get("company", "Unknown") for job in results}),
        "locations": sorted({job.get("location", "").strip() for job in results if job.get("location", "").strip()}),
    }

    # Count stats
    total = len(results)
    excellent = sum(1 for j in results if j.get("analysis", {}).get("score", 0) >= 85)
    good = sum(1 for j in results if 70 <= j.get("analysis", {}).get("score", 0) < 85)
    failed = sum(1 for j in results if j.get("analysis", {}).get("score", 0) == 0
                 and "Error" in j.get("analysis", {}).get("verdict", ""))

    # Default filter stats
    if not filter_stats:
        filter_stats = {
            "total_scanned": total,
            "skipped_recency": 0,
            "skipped_location": 0,
            "matched": total,
        }

    # Render template
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("report_template.html")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = template.render(
        generated_at=timestamp,
        resume_name=resume_name,
        model=model,
        total_jobs=total,
        excellent_count=excellent,
        good_count=good,
        failed_count=failed,
        companies=dict(companies),
        filter_stats=filter_stats,
        all_jobs=results,
        new_findings=buckets["new_findings"],
        cached_findings=buckets["cached_findings"],
        borderline_jobs=buckets["borderline_jobs"],
        waitlist_jobs=buckets["waitlist_jobs"],
        filter_options=filter_options,
        preferred_locations=preferred_locations or [],
    )

    # Write output
    filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    output_path = OUTPUT_DIR / filename

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    _cleanup_old_reports()
    logger.info(f"Report generated: {output_path}")
    return str(output_path)


def _cleanup_old_reports(keep: int = 10) -> None:
    """Delete old report files, keeping only the most recent `keep` reports."""
    reports = sorted(OUTPUT_DIR.glob("report_*.html"), key=lambda p: p.stat().st_mtime)
    for old in reports[:-keep]:
        try:
            old.unlink()
            logger.info(f"Cleaned up old report: {old.name}")
        except OSError:
            pass
