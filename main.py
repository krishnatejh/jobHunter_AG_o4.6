"""Job Hunter Agent -- main orchestrator."""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from src.analyzer import score_job
from src.config_loader import load_api_key, load_config
from src.job_cache import JobCache
from src.reporter import generate_report, prepare_results, summarize_results
from src.resume_parser import parse_resume
from src.scrapers.greenhouse import GreenhouseScraper
from src.scrapers.smart_recruiters import SmartRecruitersScraper
from src.scrapers.workday import WorkdayScraper

SCRAPERS = {
    "workday": WorkdayScraper,
    "smartrecruiters": SmartRecruitersScraper,
    "greenhouse": GreenhouseScraper,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jobhunter")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description="Job Hunter Agent - Phase 1")
    parser.add_argument(
        "--resume",
        required=True,
        help="Path to the candidate's resume (PDF)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.json (default: ./config.json)",
    )
    parser.add_argument(
        "--force-rescore",
        action="store_true",
        help="Ignore cache and re-score all jobs via LLM",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help="Optional path to write the structured run summary as JSON.",
    )
    return parser


def run_pipeline(
    resume_path: str,
    config_path: str | None = None,
    force_rescore: bool = False,
) -> dict:
    """Run the full pipeline and return a structured summary."""
    logger.info("Loading configuration...")
    config = load_config(config_path)
    api_key = load_api_key()

    model = config["model"]
    companies = config["companies"]
    recency_days = config.get("recency_days", 30)
    location_prefs = config.get("location_preferences", [])

    logger.info(f"  Model:      {model}")
    logger.info(f"  Companies:  {companies}")
    logger.info(f"  Locations:  {location_prefs}")
    logger.info(f"  Recency:    {recency_days} days")

    logger.info(f"Parsing resume: {resume_path}")
    resume_text = parse_resume(resume_path)
    logger.info(f"  Extracted {len(resume_text)} characters from resume")

    all_jobs = []
    all_filter_stats = {
        "total_scanned": 0,
        "skipped_recency": 0,
        "skipped_location": 0,
        "matched": 0,
    }

    for company_entry in companies:
        if isinstance(company_entry, dict):
            company_name = company_entry.get("name", "Unknown")
            ats_type = company_entry.get("ats", "workday")
            company_config = {
                "company_slug": company_entry.get("company_slug", ""),
                "board_token": company_entry.get("board_token", ""),
                "wd_number": company_entry.get("wd_number", ""),
                "career_slug": company_entry.get("career_slug", ""),
                "base_url": company_entry.get("base_url", ""),
                "company_name": company_name,
                "ats": ats_type,
            }
        else:
            company_name = company_entry
            ats_type = "workday"
            logger.warning(
                f"  '{company_name}' is not onboarded. "
                f'Run: python onboard.py --company "{company_name}"'
            )
            temp_scraper = WorkdayScraper()
            company_config = temp_scraper.discover(company_name)
            if not company_config:
                logger.error(f"  Could not discover career site for '{company_name}'")
                continue
            company_config["ats"] = ats_type

        scraper_class = SCRAPERS.get(ats_type, WorkdayScraper)
        scraper = scraper_class()

        logger.info("\n" + "=" * 60)
        logger.info(f"Processing company: {company_name}")
        logger.info("=" * 60)

        try:
            logger.info(
                f"  ATS: {ats_type}"
                f" | Base URL: {company_config['base_url']}"
            )

            jobs, stats = scraper.fetch_jobs(
                company_config,
                recency_days=recency_days,
                location_prefs=location_prefs if location_prefs else None,
            )

            for key in all_filter_stats:
                all_filter_stats[key] += stats.get(key, 0)

            if not jobs:
                logger.info(f"  No matching jobs found for {company_name}")
                continue

            logger.info(f"  Found {len(jobs)} matching jobs")
            all_jobs.extend(jobs)

        except Exception as exc:
            logger.error(f"  Failed to process {company_name}: {exc}")
            continue

    if not all_jobs:
        logger.warning("No jobs found across all companies. Report will be empty.")

    logger.info("\n" + "=" * 60)
    logger.info(f"Scoring {len(all_jobs)} jobs via LLM ({model})...")
    logger.info("=" * 60)

    cache = JobCache()
    cache_stats = cache.get_stats()
    logger.info(
        f"  Cache: {cache_stats['total_cached']} jobs stored, "
        f"last run: {cache_stats['last_run']}"
    )

    if force_rescore:
        logger.info("  --force-rescore: ignoring cache, re-scoring all jobs")

    scored_new = 0
    scored_cached = 0
    scored_reposted = 0
    scored_fail = 0

    for i, job in enumerate(all_jobs, 1):
        job_key = JobCache.make_job_key(job)
        company = job.get("company", "")
        posted_date = job.get("posted_date", "")

        logger.info(f"\n[{i}/{len(all_jobs)}] {job['title']} @ {company}")
        logger.info(f"  Key: {job_key}")

        if not force_rescore:
            cached_analysis, status = cache.lookup(company, job_key, posted_date)
        else:
            cached_analysis, status = None, "new"

        if status == "cached" and cached_analysis:
            job["analysis"] = cached_analysis
            job["cache_status"] = "cached"
            scored_cached += 1
            logger.info(f"  Cached (score: {cached_analysis['score']}/100)")
            continue

        if status == "reposted":
            logger.info("  Reposted - re-analyzing...")
            job["cache_status"] = "reposted"
        else:
            logger.info("  New job - analyzing...")
            job["cache_status"] = "new"

        analysis = score_job(resume_text, job, model, api_key)
        job["analysis"] = analysis

        if analysis["score"] > 0 or "Error" not in analysis.get("verdict", ""):
            if status == "reposted":
                scored_reposted += 1
            else:
                scored_new += 1
            logger.info(f"  Score: {analysis['score']}/100")
            cache.save(job, job_key, analysis)
        else:
            scored_fail += 1
            logger.warning(f"  Scoring failed: {analysis['verdict'][:80]}")

        if i < len(all_jobs):
            time.sleep(1)

    cache.close()

    logger.info(
        "\n  Scoring Summary:\n"
        f"     New:       {scored_new}\n"
        f"     Cached:    {scored_cached}\n"
        f"     Reposted:  {scored_reposted}\n"
        f"     Failed:    {scored_fail}"
    )

    logger.info("\n" + "=" * 60)
    logger.info("Generating HTML report...")
    logger.info("=" * 60)

    prepare_results(all_jobs)
    report_path = generate_report(all_jobs, resume_path, model, all_filter_stats)
    logger.info(f"\nReport saved to: {report_path}")

    report_summary = summarize_results(all_jobs)
    scores = [j["analysis"]["score"] for j in all_jobs if j["analysis"]["score"] > 0]
    average_score = sum(scores) // len(scores) if scores else 0
    highest_score = max(scores) if scores else 0

    return {
        "resume_path": str(Path(resume_path).resolve()),
        "config_path": str(Path(config_path).resolve()) if config_path else "",
        "model": model,
        "companies_scanned": len(companies),
        "jobs_found": len(all_jobs),
        "scored_new": scored_new,
        "scored_cached": scored_cached,
        "scored_reposted": scored_reposted,
        "scored_failed": scored_fail,
        "highest_score": highest_score,
        "average_score": average_score,
        "report_path": report_path,
        "filter_stats": all_filter_stats,
        "sections": report_summary["sections"],
        "ratings": report_summary["ratings"],
        "top_matches": report_summary["top_matches"],
        "force_rescore": force_rescore,
    }


def print_summary(summary: dict) -> None:
    """Print a human-readable summary block."""
    print("\n" + "=" * 60)
    print("  JOB HUNTER AGENT - SUMMARY")
    print("=" * 60)
    print(f"  Companies scanned:  {summary['companies_scanned']}")
    print(f"  Jobs found:         {summary['jobs_found']}")
    print(f"  New (LLM):          {summary['scored_new']}")
    print(f"  Cached:             {summary['scored_cached']}")
    print(f"  Reposted:           {summary['scored_reposted']}")
    print(f"  Failed:             {summary['scored_failed']}")
    if summary["jobs_found"]:
        print(f"  Highest score:      {summary['highest_score']}/100")
        print(f"  Average score:      {summary['average_score']}/100")
    print(f"  Report:             {summary['report_path']}")
    print("=" * 60 + "\n")


def main():
    args = build_arg_parser().parse_args()
    summary = run_pipeline(
        resume_path=args.resume,
        config_path=args.config,
        force_rescore=args.force_rescore,
    )

    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print_summary(summary)


if __name__ == "__main__":
    main()
