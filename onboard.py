"""Company onboarding CLI — discover career site APIs and persist to config."""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from src.scrapers.workday import WorkdayScraper
from src.scrapers.smart_recruiters import SmartRecruitersScraper
from src.scrapers.greenhouse import GreenhouseScraper

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("onboard")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

SCRAPERS = {
    "workday": WorkdayScraper,
    "smartrecruiters": SmartRecruitersScraper,
    "greenhouse": GreenhouseScraper,
}


def load_config() -> dict:
    """Load the current config.json."""
    if not CONFIG_PATH.exists():
        return {
            "model": "openrouter/hunter-alpha",
            "recency_days": 30,
            "location_preferences": [],
            "companies": [],
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    """Save config back to config.json."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info(f"  ✅ Config saved to: {CONFIG_PATH}")


def find_company(companies: list, name: str) -> int | None:
    """Find a company's index in the companies list by name (case-insensitive)."""
    for i, c in enumerate(companies):
        if isinstance(c, dict) and c.get("name", "").lower() == name.lower():
            return i
        elif isinstance(c, str) and c.lower() == name.lower():
            return i
    return None


def list_companies(config: dict) -> None:
    """List all onboarded companies."""
    companies = config.get("companies", [])

    if not companies:
        print("\n  No companies onboarded yet.")
        print("  Run: python onboard.py --company \"Company Name\"\n")
        return

    print(f"\n  {'#':<4} {'Company':<20} {'ATS':<15} {'Base URL':<50} {'Onboarded'}")
    print(f"  {'─'*4} {'─'*20} {'─'*15} {'─'*50} {'─'*20}")

    for i, c in enumerate(companies, 1):
        if isinstance(c, dict):
            print(
                f"  {i:<4} {c.get('name', '?'):<20} "
                f"{c.get('ats', '?'):<15} "
                f"{c.get('base_url', '?')[:48]+'..' if len(c.get('base_url','?'))>50 else c.get('base_url','?'):<50} "
                f"{c.get('onboarded_at', '?')[:10]}"
            )
        else:
            # Legacy string format — not yet onboarded
            print(f"  {i:<4} {c:<20} {'—':<15} {'⚠️  Not onboarded (legacy string)':<50} {'—'}")

    print()


def onboard_company(company_name: str, ats: str = "workday", custom_url: str = None, refresh: bool = False) -> None:
    """Discover a company's career site and save to config."""
    config = load_config()
    companies = config.get("companies", [])

    existing_idx = find_company(companies, company_name)

    if existing_idx is not None and not refresh:
        existing = companies[existing_idx]
        if isinstance(existing, dict) and existing.get("base_url"):
            print(f"\n  ⚠️  '{company_name}' is already onboarded:")
            print(f"     ATS:        {existing.get('ats', '?')}")
            print(f"     Base URL:   {existing.get('base_url', '?')}")
            print(f"     Onboarded:  {existing.get('onboarded_at', '?')}")
            print(f"\n  Use --refresh to re-discover endpoints.\n")
            return

    ats = ats.lower()
    if ats not in SCRAPERS:
        print(f"\n  ❌ Unsupported ATS type: '{ats}'")
        print(f"  Supported types: {', '.join(SCRAPERS.keys())}\n")
        return

    print(f"\n  🔍 Configuring career site for: {company_name} ({ats})")
    print(f"  {'─' * 50}")

    scraper_class = SCRAPERS[ats]
    scraper = scraper_class()

    if custom_url:
        print(f"  🔗 Using provided custom URL: {custom_url}")
        company_slug = company_name.split()[0].replace(",", "")
        company_config = {
            "company_name": company_name,
            "company_slug": company_slug,
            "board_token": company_slug if ats == "greenhouse" else "",
            "base_url": custom_url,
            "ats": ats,
        }
    else:
        print(f"  🔎 Attempting auto-discovery for {ats}...")
        company_config = scraper.discover(company_name)

        if not company_config:
            print(f"\n  ❌ Could not automatically find a {ats} career site for '{company_name}'.")
            print(f"     You can manually provide the API URL using the --url flag.")
            return

    print(f"\n  ✅ Found endpoint!")
    print(f"     Base URL:     {company_config['base_url']}")
    
    # Try fetching 1 job for validation
    print(f"\n  🔄 Validating endpoint (fetching 1 job)...")
    try:
        import requests
        if ats == "workday":
            api_url = (
                f"{company_config['base_url']}/wday/cxs/"
                f"{company_config['company_slug']}/{company_config['career_slug']}/jobs"
            )
            resp = requests.post(api_url, json={"limit": 1, "offset": 0}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("total", 0)
            postings = data.get("jobPostings", [])
            sample_title = postings[0].get("title", "?") if postings else None

        elif ats == "smartrecruiters":
            api_url = company_config['base_url']
            resp = requests.get(api_url, params={"limit": 1}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("totalFound", 0)
            postings = data.get("content", [])
            sample_title = postings[0].get("name", "?") if postings else None

        elif ats == "greenhouse":
            api_url = company_config["base_url"]
            resp = requests.get(api_url, params={"content": "true"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("meta", {}).get("total", 0)
            postings = data.get("jobs", [])
            sample_title = postings[0].get("title", "?") if postings else None

        if sample_title:
            print(f"     Total open jobs:  {total}")
            print(f"     Sample job:       {sample_title}")
        else:
            print(f"     ⚠️  Endpoint responded but returned 0 jobs.")
    except Exception as e:
        print(f"     ❌ Validation failed: {e}")
        print(f"     Saving anyway — you can update config.json manually.\n")

    # Build the company entry
    entry = {
        "name": company_name,
        "ats": ats,
        "base_url": company_config.get("base_url", ""),
        "company_slug": company_config.get("company_slug", ""),
        "board_token": company_config.get("board_token", ""),
        "career_slug": company_config.get("career_slug", ""),
        "wd_number": company_config.get("wd_number", ""),
        "onboarded_at": datetime.now().isoformat(timespec="seconds"),
    }

    # Insert or replace in config
    if existing_idx is not None:
        companies[existing_idx] = entry
        action = "Updated"
    else:
        companies.append(entry)
        action = "Added"

    config["companies"] = companies
    save_config(config)

    print(f"\n  🎉 {action} '{company_name}' in config.json!")
    print(f"     You can now run: python main.py --resume resume.pdf\n")


def main():
    parser = argparse.ArgumentParser(
        description="Onboard a company — discover its career site API and save to config."
    )
    parser.add_argument(
        "--company",
        type=str,
        help="Company name to onboard (e.g., 'Mastercard', 'Visa')",
    )
    parser.add_argument(
        "--ats",
        type=str,
        default="workday",
        help="ATS platform type (default: workday). Options: workday, smartrecruiters, greenhouse",
    )
    parser.add_argument(
        "--url",
        type=str,
        help="Direct API URL override (bypasses auto-discovery)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-discover endpoints for an already onboarded company",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_all",
        help="List all onboarded companies",
    )
    args = parser.parse_args()

    if args.list_all:
        config = load_config()
        list_companies(config)
        return

    if not args.company:
        parser.print_help()
        print("\nExamples:")
        print("  python onboard.py --company Mastercard")
        print("  python onboard.py --company Visa --ats smartrecruiters")
        print("  python onboard.py --company Razorpay --ats greenhouse")
        print("  python onboard.py --company \"Visa Inc\" --ats smartrecruiters --url \"https://api.smartrecruiters.com/v1/companies/Visa/postings\"")
        print("  python onboard.py --list")
        return

    onboard_company(args.company, ats=args.ats, custom_url=args.url, refresh=args.refresh)


if __name__ == "__main__":
    main()
