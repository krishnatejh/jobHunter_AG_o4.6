"""Report generator — renders HTML report from scored job results."""

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

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


def prepare_results(results: list[dict]) -> list[dict]:
    """Sort results and attach display fields used by reports and automation."""
    results.sort(key=lambda j: j.get("analysis", {}).get("score", 0), reverse=True)

    for i, job in enumerate(results):
        score = job.get("analysis", {}).get("score", 0)
        job["score_class"] = _score_class(score)
        job["score_label"] = _score_label(score)
        job["anchor_id"] = f"job-{i}"
        job["cache_status"] = job.get("cache_status", "unknown")
        job["status_label"] = {
            "new": "New",
            "cached": "Cached",
            "reposted": "Reposted",
        }.get(job["cache_status"], "Unknown")

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
    resume_path: str,
    model: str,
    filter_stats: dict | None = None,
) -> str:
    """Generate an HTML report from scored job results.

    Args:
        results: List of job dicts, each containing an 'analysis' sub-dict.
        resume_path: Path to the candidate's resume (for display).
        model: LLM model used for scoring.
        filter_stats: Dict with total_scanned, skipped_recency, skipped_location, matched.

    Returns:
        Path to the generated HTML file.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    prepare_results(results)

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
        resume_name=Path(resume_path).name,
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
    )

    # Write output
    filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    output_path = OUTPUT_DIR / filename

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Report generated: {output_path}")
    return str(output_path)
