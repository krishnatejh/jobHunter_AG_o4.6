#!/usr/bin/env python3
"""Publish the latest run summary and jobs to the yogya site."""

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from src.config_loader import load_config
from src.job_cache import JobCache


def publish_to_site(summary: dict, report_html: str, ingest_url: str, ingest_token: str) -> bool:
    """POST the run summary + jobs to the site's /api/ingest endpoint."""
    cache = JobCache()
    try:
        jobs_payload = []
        for job in summary.get("top_matches", []):
            # This is lightweight; we could also pull full jobs from cache if needed
            pass
        # For now, just send the summary; the site expects full jobs array
        # We'll build from cache for completeness
        run_id = summary.get("run_id", f"run_{summary.get('run_date', 'unknown')}")
        # Fetch full job data from cache for all reported jobs
        # Note: summary doesn't contain full job list, only top_matches
        # For a complete sync, we'd need to iterate all jobs from this run
        pass
    finally:
        cache.close()

    # Minimal implementation: send what we have
    payload = {
        "run": {
            "id": summary.get("run_id"),
            "run_date": summary.get("run_date"),
            "model": summary.get("model"),
            "fast_model": summary.get("fast_model"),
            "fast_provider": summary.get("fast_provider", "openrouter"),
            "jobs_found": summary.get("jobs_found", 0),
            "jobs_reported": summary.get("jobs_reported", 0),
            "scored_new": summary.get("scored_new", 0),
            "scored_cached": summary.get("scored_cached", 0),
            "scored_reposted": summary.get("scored_reposted", 0),
            "scored_filtered": summary.get("scored_filtered", 0),
            "scored_failed": summary.get("scored_failed", 0),
            "highest_score": summary.get("highest_score", 0),
            "average_score": summary.get("average_score", 0),
            "filter_stats": summary.get("filter_stats", {}),
            "sections": summary.get("sections", {}),
            "ratings": summary.get("ratings", {}),
            "report_html": report_html,
        },
        "jobs": [],  # Would need full job list from cache
    }

    req = urllib.request.Request(
        ingest_url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ingest_token}",
            "User-Agent": "curl/8.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            print(f"Published to site: {result}")
            return True
    except Exception as e:
        print(f"Failed to publish to site: {e}")
        if hasattr(e, "read"):
            print(f"Response: {e.read().decode()}")
        return False


if __name__ == "__main__":
    # This is a stub - the real implementation will be integrated into automation_runner.py
    print("publish_to_site.py: integrate into automation_runner.py instead")
    sys.exit(0)