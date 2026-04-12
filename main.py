"""Job Hunter Agent -- main orchestrator."""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from src.analyzer import SCORING_VERSION, score_job
from src.candidate_profile import (
    build_or_load_candidate_profile,
    candidate_preferences_from_profile,
    load_candidate_profile,
)
from src.config_loader import load_api_key, load_config
from src.job_cache import JobCache
from src.reporter import generate_report, prepare_results, summarize_results
from src.resume_parser import parse_resume
from src.screener import (
    SCREEN_SYSTEM_PROMPT,
    build_screen_reject_analysis,
    screen_job,
    should_bypass_premium_score,
)
from src.title_filter import make_skip_analysis, should_skip_job
from src.scrapers.greenhouse import GreenhouseScraper
from src.scrapers.smart_recruiters import SmartRecruitersScraper
from src.scrapers.workday import WorkdayScraper
from src.telegram_notifier import (
    format_summary_message,
    send_document,
    send_message,
)

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
        help="Path to the candidate's resume (PDF). Required only when refreshing or creating the candidate profile.",
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
        "--company",
        action="append",
        dest="company_filters",
        default=None,
        help="Only run for the specified company name. Repeat to target multiple companies.",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Process at most this many uncached jobs in this run. Cached jobs do not count toward the limit.",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help="Optional path to write the structured run summary as JSON.",
    )
    parser.add_argument(
        "--show-job-debug",
        default=None,
        help="Print the cached debug payload for a specific job id and exit.",
    )
    parser.add_argument(
        "--refresh-candidate-profile",
        action="store_true",
        help="Regenerate the sanitized candidate profile from the resume before running.",
    )
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Run locally without Telegram notification even if Telegram secrets are present.",
    )
    return parser


def run_pipeline(
    resume_path: str | None,
    config_path: str | None = None,
    force_rescore: bool = False,
    company_filters: list[str] | None = None,
    refresh_candidate_profile: bool = False,
    max_jobs: int | None = None,
) -> dict:
    """Run the full pipeline and return a structured summary."""
    logger.info("Loading configuration...")
    config = load_config(config_path)
    premium_api_key = load_api_key("premium")
    fast_api_key = load_api_key("fast")

    model = config["premium_model"]
    fast_model = config["fast_model"]
    companies = config["companies"]
    recency_days = config.get("recency_days", 30)
    legacy_candidate_preferences = {}
    candidate_profile_path = config.get("candidate_profile_path")
    selected_companies = companies

    if company_filters:
        normalized_filters = {name.strip().lower() for name in company_filters if name.strip()}

        def company_name_for(entry: dict | str) -> str:
            if isinstance(entry, dict):
                return entry.get("name", "")
            return entry

        selected_companies = [
            entry for entry in companies
            if company_name_for(entry).strip().lower() in normalized_filters
        ]

        missing_companies = sorted(
            normalized_filters
            - {
                company_name_for(entry).strip().lower()
                for entry in selected_companies
            }
        )
        if missing_companies:
            raise ValueError(
                "Requested company not found in config.json: "
                + ", ".join(missing_companies)
            )

    logger.info(f"  Premium model: {model}")
    logger.info(f"  Fast model:    {fast_model}")
    logger.info(f"  Companies:  {selected_companies}")
    logger.info(f"  Recency:    {recency_days} days")
    if max_jobs:
        logger.info(f"  Max uncached jobs this run: {max_jobs}")

    profile_path_obj = Path(candidate_profile_path) if candidate_profile_path else None
    existing_profile = (
        load_candidate_profile(profile_path_obj)
        if profile_path_obj and profile_path_obj.exists()
        else None
    )

    if refresh_candidate_profile or not existing_profile:
        if not resume_path:
            raise ValueError(
                "--resume is required when refreshing the candidate profile or when no candidate profile exists."
            )
        logger.info(f"Parsing resume: {resume_path}")
        resume_text = parse_resume(resume_path)
        logger.info(f"  Extracted {len(resume_text)} characters from resume")
        seed_profile = existing_profile or {}
        seed_candidate_preferences = (
            candidate_preferences_from_profile(seed_profile)
            if seed_profile
            else legacy_candidate_preferences
        )
        seed_locations = seed_profile.get("preferred_locations", []) or []
        candidate_profile = build_or_load_candidate_profile(
            resume_text,
            seed_candidate_preferences,
            seed_locations,
            resume_path,
            profile_path=candidate_profile_path,
            force_refresh=refresh_candidate_profile,
        )
    else:
        candidate_profile = existing_profile

    runtime_resume_text = (
        resume_text
        if "resume_text" in locals()
        else candidate_profile.get("resume_excerpt_redacted", "")
    )
    resume_display_name = (
        Path(resume_path).name
        if resume_path
        else candidate_profile.get("source_resume_file", "candidate profile")
    )

    runtime_candidate_preferences = candidate_preferences_from_profile(candidate_profile)
    runtime_location_prefs = candidate_profile.get("preferred_locations", []) or []
    logger.info(
        "  Candidate profile: %s | summary: %s | target roles: %s | preferred locations: %s",
        candidate_profile_path,
        candidate_profile.get("candidate_summary_short", ""),
        ", ".join(runtime_candidate_preferences.get("target_roles", [])[:4]) or "Not provided",
        ", ".join(runtime_location_prefs[:4]) or "Not provided",
    )

    all_jobs = []
    all_filter_stats = {
        "total_scanned": 0,
        "skipped_recency": 0,
        "skipped_location": 0,
        "matched": 0,
    }

    for company_entry in selected_companies:
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
                location_prefs=runtime_location_prefs if runtime_location_prefs else None,
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
        cache_lookup_results = [(None, "new") for _ in all_jobs]
        cached_ready = 0
        uncached_remaining = len(all_jobs)
    else:
        cache_lookup_results = []
        cached_ready = 0
        uncached_remaining = 0
        for job in all_jobs:
            cached_analysis, cache_status = cache.lookup(
                company=job.get("company", ""),
                job_key=JobCache.make_job_key(job),
                posted_date=job.get("posted_date", ""),
                scoring_version=SCORING_VERSION,
            )
            cache_lookup_results.append((cached_analysis, cache_status))
            if cache_status == "cached" and cached_analysis:
                cached_ready += 1
            else:
                uncached_remaining += 1

    logger.info(
        "  Backlog: matched=%s | cached-ready=%s | uncached/reposted=%s",
        len(all_jobs),
        cached_ready,
        uncached_remaining,
    )
    if max_jobs:
        logger.info(
            "  This run will process up to %s uncached jobs before stopping.",
            min(max_jobs, uncached_remaining),
        )

    scored_new = 0
    scored_cached = 0
    scored_reposted = 0
    scored_fail = 0
    scored_filtered = 0
    processed_uncached = 0
    max_jobs_reached = False
    reported_jobs = []

    for i, job in enumerate(all_jobs, 1):
        job_key = JobCache.make_job_key(job)
        company = job.get("company", "")
        posted_date = job.get("posted_date", "")

        logger.info(f"\n[{i}/{len(all_jobs)}] {job['title']} @ {company}")
        logger.info(f"  Key: {job_key}")

        cached_analysis, cache_status = cache_lookup_results[i - 1]

        if cache_status == "cached" and cached_analysis:
            job["analysis"] = cached_analysis
            job["cache_status"] = "cached"
            scored_cached += 1
            logger.info(f"  Cached (score: {cached_analysis['score']}/100)")
            reported_jobs.append(job)
            continue

        if max_jobs and processed_uncached >= max_jobs:
            logger.info(
                "Reached --max-jobs=%s after processing %s uncached jobs; stopping early.",
                max_jobs,
                processed_uncached,
            )
            max_jobs_reached = True
            break

        processed_uncached += 1

        skip, skip_reason = should_skip_job(job.get("title", ""), runtime_candidate_preferences)
        if skip:
            logger.info(f"  Skipped (title filter: {skip_reason})")
            job["analysis"] = make_skip_analysis(skip_reason)
            job["cache_status"] = "filtered"
            scored_filtered += 1
            cache.save(job, job_key, job["analysis"])
            reported_jobs.append(job)
            continue

        elif cache_status == "reposted":
            logger.info("  Reposted - re-analyzing...")
            job["analysis"] = cached_analysis  # Though None, kept for clarity
            job["cache_status"] = "reposted"
        else:
            logger.info("  New job - analyzing...")
            job["cache_status"] = "new"

        screen_result = screen_job(
            candidate_profile,
            job,
            fast_model,
            fast_api_key,
            preferred_locations=runtime_location_prefs,
            candidate_preferences=runtime_candidate_preferences,
        )
        logger.info(
            "  Screen: %s (%.2f) | %s",
            ("Review" if str(screen_result.get("decision", "review")).strip().lower() in {"review", "borderline"} else str(screen_result.get("decision", "review")).strip().title()),
            screen_result.get("confidence", 0.0),
            screen_result.get("reason", ""),
        )

        if should_bypass_premium_score(screen_result):
            logger.info("  Premium scoring bypassed due to confident screen reject")
            analysis = build_screen_reject_analysis(screen_result, SCORING_VERSION)
            analysis["prepared_job_description"] = job.get("description", "")
            job["analysis"] = analysis
            job["cache_status"] = "filtered"
            scored_filtered += 1
            cache.save(job, job_key, analysis)
            reported_jobs.append(job)
            continue

        analysis = score_job(
            runtime_resume_text,
            job,
            model,
            premium_api_key,
            preferred_locations=runtime_location_prefs,
            candidate_preferences=runtime_candidate_preferences,
            candidate_profile=candidate_profile,
        )
        analysis["screen_result"] = screen_result
        analysis["screen_system_prompt"] = SCREEN_SYSTEM_PROMPT
        analysis["screen_user_prompt"] = screen_result.get("raw_prompt", "")
        analysis["screen_raw_output"] = screen_result.get("raw_content", "")
        analysis["screen_raw_response_json"] = screen_result.get("raw_response_json", "")
        analysis["screen_model_used"] = screen_result.get("model_used", "")
        analysis["screen_attempts"] = screen_result.get("screen_attempts", [])
        analysis.setdefault("qa_result", {})
        analysis.setdefault("qa_system_prompt", "")
        analysis.setdefault("qa_user_prompt", "")
        analysis.setdefault("qa_raw_output", "")
        analysis.setdefault("qa_raw_response_json", "")
        analysis.setdefault("qa_model_used", "")
        job["analysis"] = analysis

        if analysis["score"] > 0 or "Error" not in analysis.get("verdict", ""):
            if cache_status == "reposted":
                scored_reposted += 1
            else:
                scored_new += 1
            logger.info(f"  Score: {analysis['score']}/100")
            cache.save(job, job_key, analysis)
        else:
            scored_fail += 1
            logger.warning(f"  Scoring failed: {analysis['verdict'][:80]}")

        reported_jobs.append(job)

        if i < len(all_jobs):
            time.sleep(1)

    cache.close()

    logger.info(
        "\n  Scoring Summary:\n"
        f"     New:       {scored_new}\n"
        f"     Cached:    {scored_cached}\n"
        f"     Reposted:  {scored_reposted}\n"
        f"     Filtered:  {scored_filtered}\n"
        f"     Failed:    {scored_fail}"
    )

    logger.info("\n" + "=" * 60)
    logger.info("Generating HTML report...")
    logger.info("=" * 60)

    prepare_results(reported_jobs)
    report_path = generate_report(
        reported_jobs,
        resume_display_name,
        model,
        all_filter_stats,
        preferred_locations=runtime_location_prefs,
    )
    logger.info(f"\nReport saved to: {report_path}")

    report_summary = summarize_results(reported_jobs)
    scores = [j["analysis"]["score"] for j in reported_jobs if j["analysis"]["score"] > 0]
    average_score = sum(scores) // len(scores) if scores else 0
    highest_score = max(scores) if scores else 0

    return {
        "resume_path": str(Path(resume_path).resolve()) if resume_path else "",
        "resume_name": resume_display_name,
        "config_path": str(Path(config_path).resolve()) if config_path else "",
        "candidate_profile_path": str(Path(candidate_profile_path).resolve()) if candidate_profile_path else "",
        "model": model,
        "companies_scanned": len(selected_companies),
        "jobs_found": len(all_jobs),
        "jobs_reported": len(reported_jobs),
        "scored_new": scored_new,
        "scored_cached": scored_cached,
        "scored_reposted": scored_reposted,
        "scored_filtered": scored_filtered,
        "scored_failed": scored_fail,
        "highest_score": highest_score,
        "average_score": average_score,
        "report_path": report_path,
        "filter_stats": all_filter_stats,
        "sections": report_summary["sections"],
        "ratings": report_summary["ratings"],
        "top_matches": report_summary["top_matches"],
        "force_rescore": force_rescore,
        "company_filters": company_filters or [],
        "max_jobs": max_jobs or 0,
        "processed_uncached": processed_uncached,
        "max_jobs_reached": max_jobs_reached,
        "run_source": "local",
    }


def print_summary(summary: dict) -> None:
    """Print a human-readable summary block."""
    print("\n" + "=" * 60)
    print("  JOB HUNTER AGENT - SUMMARY")
    print("=" * 60)
    print(f"  Companies scanned:  {summary['companies_scanned']}")
    print(f"  Jobs found:         {summary['jobs_found']}")
    if "jobs_reported" in summary and summary["jobs_reported"] != summary["jobs_found"]:
        print(f"  Jobs in report:     {summary['jobs_reported']}")
    print(f"  New (LLM):          {summary['scored_new']}")
    print(f"  Cached:             {summary['scored_cached']}")
    print(f"  Reposted:           {summary['scored_reposted']}")
    print(f"  Filtered:           {summary['scored_filtered']}")
    print(f"  Failed:             {summary['scored_failed']}")
    if summary.get("max_jobs"):
        print(f"  Max uncached jobs:  {summary['max_jobs']}")
        print(f"  Processed uncached: {summary.get('processed_uncached', 0)}")
    if summary.get("candidate_profile_path"):
        print(f"  Candidate profile:  {summary['candidate_profile_path']}")
    if summary["jobs_found"]:
        print(f"  Highest score:      {summary['highest_score']}/100")
        print(f"  Average score:      {summary['average_score']}/100")
    print(f"  Report:             {summary['report_path']}")
    print("=" * 60 + "\n")


def print_job_debug(job_debug: dict) -> None:
    """Print a cached job debug payload for inspection."""
    print(json.dumps(job_debug, indent=2, ensure_ascii=False))


def maybe_send_telegram(summary: dict) -> None:
    """Send summary and report to Telegram if secrets are present."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.info("Telegram credentials missing; skipping local Telegram delivery.")
        return

    try:
        message = format_summary_message(summary)
        send_message(bot_token, chat_id, message)
        send_document(
            bot_token,
            chat_id,
            summary["report_path"],
            caption="Job Hunter report",
        )
        logger.info("Telegram notification sent.")
    except Exception as exc:
        logger.warning(f"Failed to send Telegram notification: {exc}")


def main():
    args = build_arg_parser().parse_args()

    if args.show_job_debug:
        cache = JobCache()
        job_debug = cache.get_job_debug(args.show_job_debug)
        cache.close()
        if not job_debug:
            raise SystemExit(f"No cached job found for job id: {args.show_job_debug}")
        print_job_debug(job_debug)
        return

    summary = run_pipeline(
        resume_path=args.resume,
        config_path=args.config,
        force_rescore=args.force_rescore,
        company_filters=args.company_filters,
        refresh_candidate_profile=args.refresh_candidate_profile,
        max_jobs=args.max_jobs,
    )

    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print_summary(summary)
    if not args.skip_telegram:
        maybe_send_telegram(summary)
    else:
        logger.info("Telegram notification skipped by flag.")


if __name__ == "__main__":
    main()
