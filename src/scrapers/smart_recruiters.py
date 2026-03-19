"""SmartRecruiters job scraper — auto-discovers and fetches jobs via SmartRecruiters' public API."""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


class SmartRecruitersScraper(BaseScraper):
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    def discover(self, company_name: str) -> dict | None:
        """Discover if a company uses SmartRecruiters by pinging its public API."""
        # Sanitize company name (e.g., 'Visa Inc' -> 'Visa')
        slug = company_name.split()[0].replace(",", "")
        api_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"

        try:
            resp = self._session.get(api_url, params={"limit": 1}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "totalFound" in data:
                    return {
                        "company_name": company_name,
                        "company_slug": slug,
                        "base_url": api_url,
                        "ats": "smartrecruiters",
                    }
        except requests.RequestException:
            pass

        return None

    def fetch_jobs(
        self,
        company_config: dict,
        recency_days: int = 30,
        location_prefs: list[str] | None = None,
    ) -> tuple[list[dict], dict]:
        """Fetch job listings from SmartRecruiters API and apply filters."""
        slug = company_config["company_slug"]
        api_url = company_config["base_url"]
        
        # Determine cutoff date
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=recency_days)

        all_jobs = []
        offset = 0
        limit = 100
        total_scanned = 0
        skipped_recency = 0
        skipped_location = 0

        logger.info(
            f"Fetching jobs from {slug} via SmartRecruiters (recency={recency_days}d, "
            f"locations={location_prefs})..."
        )

        while True:
            try:
                resp = self._session.get(
                    api_url,
                    params={"limit": limit, "offset": offset},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                logger.error(f"API request failed at offset {offset}: {e}")
                break

            postings = data.get("content", [])
            if not postings:
                break

            total = data.get("totalFound", 0)
            logger.info(
                f"  Scanning {min(offset + len(postings), total)}/{total} listings... "
                f"(matched so far: {len(all_jobs)})"
            )

            for posting in postings:
                total_scanned += 1
                job, skip_reason = self._process_posting(
                    posting, cutoff_date, location_prefs
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
        cutoff_date: datetime,
        location_prefs: list[str] | None,
    ) -> tuple[dict | None, str]:
        """Process a single job — apply filters, then fetch full details if it passes."""
        title = posting.get("name", "Unknown")
        post_id = posting.get("id", "")
        slug = posting.get("company", {}).get("identifier", "")
        
        # SmartRecruiters provides direct application URLs, but we optionally fetch deep API data
        # Let's map dates
        released_date_str = posting.get("releasedDate", "")
        loc_dict = posting.get("location", {})
        
        locations_text = f"{loc_dict.get('city', '')}, {loc_dict.get('region', '')}, {loc_dict.get('country', '')}"
        remote = loc_dict.get("remote", False)
        if remote:
            locations_text += " (Remote)"

        # --- Recency filter ---
        if released_date_str:
            try:
                # e.g., "2026-03-15T04:54:48.786Z"
                post_date = datetime.fromisoformat(released_date_str.replace("Z", "+00:00"))
                if post_date < cutoff_date:
                    return None, "recency"
            except (ValueError, TypeError):
                pass

        # --- Location filter ---
        if location_prefs and locations_text:
            loc_lower = locations_text.lower()
            if not any(pref.lower() in loc_lower for pref in location_prefs):
                return None, "location"

        # --- Fetch Full Job Details ---
        try:
            detail_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{post_id}"
            resp = self._session.get(detail_url, timeout=10)
            resp.raise_for_status()
            detail_data = resp.json()
            
            # Combine sections (companyDescription, jobDescription, qualifications, additionalInformation)
            sections = []
            ad_sections = detail_data.get("jobAd", {}).get("sections", {})
            for key in ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]:
                sections.append(ad_sections.get(key, {}).get("text", ""))
                
            full_html = "\n\n".join(filter(None, sections))
            # Strip tags
            full_text = BeautifulSoup(full_html, "html.parser").get_text(separator="\n").strip()
        except requests.RequestException as e:
            logger.warning(f"  Failed to fetch details for task {post_id}: {e}")
            return None, ""

        return {
            "title": title,
            "job_req_id": post_id,
            "url": f"https://jobs.smartrecruiters.com/{slug}/{post_id}",
            "location": locations_text,
            "posted_date": released_date_str[:10] if released_date_str else "",
            "description": full_text,
            "company": slug
        }, ""
