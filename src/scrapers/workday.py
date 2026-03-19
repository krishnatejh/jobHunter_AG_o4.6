"""Workday job scraper — auto-discovers and fetches jobs via Workday's internal API."""

import json
import re
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "company_cache.json"

# Realistic browser headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Common Workday datacenter numbers
WD_NUMBERS = ["wd1", "wd2", "wd3", "wd5", "wd12"]

# Common career site slugs to try during brute-force discovery
COMMON_SLUGS = [
    "CorporateCareers",
    "External_Career_Site",
    "en-US",
    "careers",
    "Careers",
    "External_Careers",
    "External",
    "Jobs",
    "job",
    "Career",
    "jobs",
]


class WorkdayScraper(BaseScraper):
    """Scraper for companies using Workday as their ATS."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._cache = self._load_cache()

    # ------------------------------------------------------------------ #
    #  Cache management
    # ------------------------------------------------------------------ #

    def _load_cache(self) -> dict:
        """Load the persistent company cache from disk."""
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_cache(self) -> None:
        """Persist the company cache to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2)

    # ------------------------------------------------------------------ #
    #  Discovery
    # ------------------------------------------------------------------ #

    def discover(self, company_name: str) -> dict:
        """Auto-discover a company's Workday careers API details.

        Strategy:
        1. Check local cache first.
        2. Search DuckDuckGo for the company's myworkdayjobs.com URL.
        3. Parse the URL to extract company_slug, wd_number, career_slug.
        4. If search fails, brute-force common patterns.
        5. Cache the result.

        Returns:
            dict with keys: company_name, company_slug, wd_number, career_slug, base_url
        """
        cache_key = company_name.lower().strip()

        if cache_key in self._cache:
            logger.info(f"Cache hit for '{company_name}'")
            return self._cache[cache_key]

        logger.info(f"Discovering Workday URL for '{company_name}'...")

        # Strategy 1: DuckDuckGo search
        result = self._discover_via_search(company_name)

        # Strategy 2: Probe wd subdomains and detect career slug from HTML
        if not result:
            logger.info("Search discovery failed, trying HTML probe...")
            result = self._discover_via_html_probe(company_name)

        # Strategy 3: Brute-force API endpoints
        if not result:
            logger.info("HTML probe failed, trying brute-force API...")
            result = self._discover_via_bruteforce(company_name)

        if not result:
            raise RuntimeError(
                f"Could not discover Workday careers page for '{company_name}'. "
                "The company may not use Workday, or the URL pattern is non-standard."
            )

        result["company_name"] = company_name
        self._cache[cache_key] = result
        self._save_cache()
        logger.info(f"Discovered: {result}")
        return result

    def _discover_via_search(self, company_name: str) -> dict | None:
        """Search DuckDuckGo for the company's Workday URL."""
        query = f"{company_name} careers site:myworkdayjobs.com"
        search_url = "https://html.duckduckgo.com/html/"

        try:
            resp = self._session.get(
                search_url,
                params={"q": query},
                headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept": "text/html",
                    "Content-Type": "text/html",
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # DuckDuckGo HTML results — extract all href attributes
        all_hrefs = []
        for link in soup.find_all("a"):
            href = link.get("href", "")
            all_hrefs.append(href)
            parsed = self._parse_workday_url(href)
            if parsed:
                return parsed

        # DuckDuckGo sometimes wraps URLs as redirect params
        # e.g., //duckduckgo.com/l/?uddg=https%3A%2F%2Fcompany.wd1.myworkdayjobs.com%2F...
        from urllib.parse import unquote, urlparse, parse_qs
        for href in all_hrefs:
            if "uddg=" in href:
                try:
                    parsed_url = urlparse(href)
                    params = parse_qs(parsed_url.query)
                    real_url = params.get("uddg", [""])[0]
                    if real_url:
                        real_url = unquote(real_url)
                        parsed = self._parse_workday_url(real_url)
                        if parsed:
                            return parsed
                except Exception:
                    pass

        # Also try extracting URLs from the raw text
        urls = re.findall(
            r"https?://[\w.-]+\.myworkdayjobs\.com[^\s\"'<>]*", resp.text
        )
        for url in urls:
            parsed = self._parse_workday_url(url)
            if parsed:
                return parsed

        return None

    def _parse_workday_url(self, url: str) -> dict | None:
        """Extract company_slug, wd_number, and career_slug from a Workday URL.

        Expected patterns:
            https://{company_slug}.{wd_number}.myworkdayjobs.com/.../{career_slug}/...
            https://{company_slug}.{wd_number}.myworkdayjobs.com/{lang}/{career_slug}
        """
        pattern = re.compile(
            r"https?://"
            r"(?P<company_slug>[\w-]+)\."
            r"(?P<wd_number>wd\d+)\."
            r"myworkdayjobs\.com"
            r"(?:/[a-z]{2}-[A-Z]{2})?"  # optional locale like /en-US
            r"/(?P<career_slug>[A-Za-z0-9_-]+)"
        )
        match = pattern.search(url)
        if not match:
            return None

        d = match.groupdict()
        base_url = (
            f"https://{d['company_slug']}.{d['wd_number']}.myworkdayjobs.com"
        )
        return {
            "company_slug": d["company_slug"],
            "wd_number": d["wd_number"],
            "career_slug": d["career_slug"],
            "base_url": base_url,
        }

    def _discover_via_html_probe(self, company_name: str) -> dict | None:
        """Probe Workday subdomains and extract career slug from the HTML/redirect."""
        company_slugs = list(dict.fromkeys([
            company_name.lower().replace(" ", ""),
            company_name.lower().replace(" ", "_"),
            company_name.lower().replace(" ", "-"),
        ]))

        for company_slug in company_slugs:
            for wd in WD_NUMBERS:
                base = f"https://{company_slug}.{wd}.myworkdayjobs.com"
                try:
                    resp = self._session.get(
                        base,
                        headers={"Accept": "text/html", "User-Agent": HEADERS["User-Agent"]},
                        timeout=6,
                        allow_redirects=True,
                    )
                    # Check redirect chain for career slug
                    for r in resp.history + [resp]:
                        parsed = self._parse_workday_url(str(r.url))
                        if parsed:
                            # Verify the API actually works
                            if self._verify_api(parsed):
                                return parsed

                    # Also search the HTML body for career site links
                    if resp.status_code == 200 and resp.text:
                        found_urls = re.findall(
                            r'["\']/((?:en-US/)?[A-Za-z0-9_-]+)(?:/|["\'])',
                            resp.text
                        )
                        for slug_candidate in found_urls:
                            # Strip locale prefix
                            slug_clean = re.sub(r'^[a-z]{2}-[A-Z]{2}/', '', slug_candidate)
                            if slug_clean and len(slug_clean) > 2:
                                candidate = {
                                    "company_slug": company_slug,
                                    "wd_number": wd,
                                    "career_slug": slug_clean,
                                    "base_url": base,
                                }
                                if self._verify_api(candidate):
                                    return candidate

                except requests.RequestException:
                    continue
                time.sleep(0.3)

        return None

    def _verify_api(self, config: dict) -> bool:
        """Quick check that the jobs API endpoint actually returns data."""
        api_url = (
            f"{config['base_url']}/wday/cxs/"
            f"{config['company_slug']}/{config['career_slug']}/jobs"
        )
        try:
            resp = self._session.post(
                api_url,
                json={"limit": 1, "offset": 0},
                timeout=6,
            )
            if resp.status_code == 200:
                data = resp.json()
                return "jobPostings" in data or "total" in data
        except requests.RequestException:
            pass
        return False

    def _discover_via_bruteforce(self, company_name: str) -> dict | None:
        """Try common Workday URL patterns to find the right one."""
        company_slugs = list(dict.fromkeys([
            company_name.lower().replace(" ", ""),
            company_name.lower().replace(" ", "_"),
            company_name.lower().replace(" ", "-"),
        ]))

        for company_slug in company_slugs:
            for wd in WD_NUMBERS:
                for career_slug in COMMON_SLUGS:
                    api_url = (
                        f"https://{company_slug}.{wd}.myworkdayjobs.com"
                        f"/wday/cxs/{company_slug}/{career_slug}/jobs"
                    )
                    try:
                        resp = self._session.post(
                            api_url,
                            json={"limit": 1, "offset": 0},
                            timeout=8,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if "jobPostings" in data or "total" in data:
                                return {
                                    "company_slug": company_slug,
                                    "wd_number": wd,
                                    "career_slug": career_slug,
                                    "base_url": f"https://{company_slug}.{wd}.myworkdayjobs.com",
                                }
                    except requests.RequestException:
                        continue
                    # Be polite — small delay between brute-force attempts
                    time.sleep(0.2)

        return None

    # ------------------------------------------------------------------ #
    #  Job fetching
    # ------------------------------------------------------------------ #

    def fetch_jobs(
        self,
        company_config: dict,
        recency_days: int = 30,
        location_prefs: list[str] | None = None,
    ) -> tuple[list[dict], dict]:
        """Fetch job listings from Workday's internal API.

        Paginates through all results, fetches details, and applies filters.

        Returns:
            Tuple of (matched_jobs_list, filter_stats_dict).
        """
        slug = company_config["company_slug"]
        wd = company_config["wd_number"]
        career = company_config["career_slug"]
        base = company_config["base_url"]

        api_url = f"{base}/wday/cxs/{slug}/{career}/jobs"
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=recency_days)

        all_jobs = []
        offset = 0
        limit = 20
        total_scanned = 0
        skipped_recency = 0
        skipped_location = 0

        logger.info(
            f"Fetching jobs from {slug} (recency={recency_days}d, "
            f"locations={location_prefs})..."
        )

        while True:
            try:
                resp = self._session.post(
                    api_url,
                    json={"limit": limit, "offset": offset},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                logger.error(f"API request failed at offset {offset}: {e}")
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            total = data.get("total", 0)
            logger.info(
                f"  Scanning {offset + len(postings)}/{total} listings... "
                f"(matched so far: {len(all_jobs)})"
            )

            for posting in postings:
                total_scanned += 1
                job, skip_reason = self._process_posting(
                    posting, company_config, cutoff_date, location_prefs
                )
                if job:
                    all_jobs.append(job)
                elif skip_reason == "recency":
                    skipped_recency += 1
                elif skip_reason == "location":
                    skipped_location += 1

            offset += limit
            if offset >= total:
                break

            # Polite delay between pages
            time.sleep(0.5)

        filter_stats = {
            "total_scanned": total_scanned,
            "skipped_recency": skipped_recency,
            "skipped_location": skipped_location,
            "matched": len(all_jobs),
        }

        logger.info(
            f"\n  📊 Filter Summary for '{company_config.get('company_name', slug)}':\n"
            f"     Total scanned:     {total_scanned}\n"
            f"     Skipped (recency): {skipped_recency}\n"
            f"     Skipped (location):{skipped_location}\n"
            f"     ✅ Matched:         {len(all_jobs)}"
        )
        return all_jobs, filter_stats

    def _process_posting(
        self,
        posting: dict,
        company_config: dict,
        cutoff_date: datetime,
        location_prefs: list[str] | None,
    ) -> tuple[dict | None, str]:
        """Process a single job posting — fetch details and apply filters.

        Returns:
            Tuple of (job_dict_or_None, skip_reason).
            skip_reason is 'recency', 'location', or '' if not skipped.
        """
        title = posting.get("title", "Unknown")
        external_path = posting.get("externalPath", "")
        posted_on = posting.get("postedOn", "")
        locations_text = posting.get("locationsText", "")
        bullet_fields = posting.get("bulletFields", [])

        # --- Recency filter ---
        if posted_on:
            post_date = None
            try:
                # Try ISO format first
                post_date = datetime.fromisoformat(
                    posted_on.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                # Try relative Workday string (e.g., "Posted 2 Days Ago", "Posted Yesterday")
                po_lower = posted_on.lower()
                if "today" in po_lower:
                    post_date = datetime.now(timezone.utc)
                elif "yesterday" in po_lower:
                    post_date = datetime.now(timezone.utc) - timedelta(days=1)
                elif "days ago" in po_lower:
                    match = re.search(r'(\d+)', po_lower)
                    if match:
                        post_date = datetime.now(timezone.utc) - timedelta(days=int(match.group(1)))

            if post_date and post_date < cutoff_date:
                return None, "recency"

        # --- Location filter ---
        if location_prefs and locations_text:
            loc_lower = locations_text.lower()
            if not any(pref.lower() in loc_lower for pref in location_prefs):
                return None, "location"

        # --- Fetch full job details (description + metadata) ---
        details = self._fetch_job_details(external_path, company_config)

        # Build browsable career site URL (includes career slug)
        career_slug = company_config["career_slug"]
        job_url = f"{company_config['base_url']}/en-US/{career_slug}{external_path}"

        return {
            "title": title,
            "url": job_url,
            "location": locations_text,
            "posted_date": details.get("start_date", posted_on),
            "end_date": details.get("end_date", ""),
            "time_left": details.get("time_left", ""),
            "job_req_id": details.get("job_req_id", ""),
            "time_type": details.get("time_type", ""),
            "description": details.get("description", ""),
            "bullet_fields": bullet_fields,
            "company": company_config.get("company_name", company_config["company_slug"]),
        }, ""

    def _fetch_job_details(self, external_path: str, company_config: dict) -> dict:
        """Fetch the full job description and metadata for a specific posting.

        Returns:
            Dict with keys: description, job_req_id, start_date, end_date,
            time_left, time_type.
        """
        empty = {"description": "", "job_req_id": "", "start_date": "",
                 "end_date": "", "time_left": "", "time_type": ""}
        if not external_path:
            return empty

        slug = company_config["company_slug"]
        career = company_config["career_slug"]
        base = company_config["base_url"]

        detail_url = f"{base}/wday/cxs/{slug}/{career}{external_path}"

        try:
            resp = self._session.get(detail_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            job_info = data.get("jobPostingInfo", {})
            description = job_info.get("jobDescription", "")

            # Strip HTML tags for cleaner text
            if description:
                description = BeautifulSoup(description, "html.parser").get_text(
                    separator="\n"
                )

            return {
                "description": description.strip(),
                "job_req_id": job_info.get("jobReqId", ""),
                "start_date": job_info.get("startDate", ""),
                "end_date": job_info.get("endDate", ""),
                "time_left": job_info.get("timeLeftToApply", ""),
                "time_type": job_info.get("timeType", ""),
            }

        except requests.RequestException as e:
            logger.warning(f"Failed to fetch details for {external_path}: {e}")
            return empty

        finally:
            time.sleep(0.3)  # Polite delay
