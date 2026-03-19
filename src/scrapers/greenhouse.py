"""Greenhouse job scraper -- auto-discovers and fetches jobs via Greenhouse's Job Board API."""

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

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

SEARCH_URL = "https://html.duckduckgo.com/html/"
BOARD_API_ROOT = "https://boards-api.greenhouse.io/v1/boards"
LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "private",
    "pvt",
    "corp",
    "corporation",
    "company",
    "co",
}


class GreenhouseScraper(BaseScraper):
    """Scraper for companies using Greenhouse as their ATS."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    def discover(self, company_name: str) -> dict | None:
        """Discover a Greenhouse board token for the company."""
        logger.info(f"Discovering Greenhouse board for '{company_name}'...")

        for board_token in self._discover_tokens_via_search(company_name):
            config = self._build_config(company_name, board_token)
            if self._verify_board(config):
                logger.info(f"Discovered Greenhouse board: {config}")
                return config

        for board_token in self._generate_candidate_tokens(company_name):
            config = self._build_config(company_name, board_token)
            if self._verify_board(config):
                logger.info(f"Discovered Greenhouse board via slug probe: {config}")
                return config

        return None

    def _discover_tokens_via_search(self, company_name: str) -> list[str]:
        query = (
            f'{company_name} careers '
            "(site:job-boards.greenhouse.io OR site:boards.greenhouse.io)"
        )

        try:
            resp = self._session.get(
                SEARCH_URL,
                params={"q": query},
                headers={"Accept": "text/html", "User-Agent": HEADERS["User-Agent"]},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning(f"Greenhouse search discovery failed: {exc}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        tokens = []

        for link in soup.find_all("a"):
            href = link.get("href", "")
            text = link.get_text(" ", strip=True)
            token = self._extract_board_token(href)
            if token and self._looks_like_match(company_name, token, text):
                tokens.append(token)

            if "uddg=" not in href:
                continue

            try:
                parsed_url = urlparse(href)
                real_url = parse_qs(parsed_url.query).get("uddg", [""])[0]
                token = self._extract_board_token(unquote(real_url))
                if token and self._looks_like_match(company_name, token, text):
                    tokens.append(token)
            except Exception:
                continue

        raw_urls = re.findall(
            r"https?://(?:job-boards|boards)\.greenhouse\.io/[^\s\"'<>]+",
            resp.text,
        )
        for url in raw_urls:
            token = self._extract_board_token(url)
            if token and self._looks_like_match(company_name, token, url):
                tokens.append(token)

        return list(dict.fromkeys(tokens))

    def _extract_board_token(self, url: str) -> str | None:
        match = re.search(
            r"https?://(?:job-boards|boards)\.greenhouse\.io/"
            r"(?P<token>[A-Za-z0-9_-]+)",
            url,
        )
        if not match:
            return None
        return match.group("token")

    def _looks_like_match(self, company_name: str, board_token: str, context: str) -> bool:
        token_norm = self._normalize(board_token)
        company_norm = self._normalize(company_name)
        context_norm = self._normalize(context)

        if token_norm == company_norm:
            return True

        if company_norm and company_norm in token_norm:
            return True

        if company_norm and company_norm in context_norm:
            return True

        core_name = self._normalize(" ".join(self._company_tokens(company_name, strip_legal=True)))
        return bool(core_name and core_name in token_norm)

    def _generate_candidate_tokens(self, company_name: str) -> list[str]:
        raw_tokens = self._company_tokens(company_name, strip_legal=False)
        core_tokens = self._company_tokens(company_name, strip_legal=True)
        candidates = [
            "".join(raw_tokens),
            "-".join(raw_tokens),
            "_".join(raw_tokens),
            "".join(core_tokens),
            "-".join(core_tokens),
            "_".join(core_tokens),
        ]
        return [candidate for candidate in dict.fromkeys(candidates) if candidate]

    def _company_tokens(self, company_name: str, strip_legal: bool) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", company_name.lower())
        if strip_legal:
            tokens = [token for token in tokens if token not in LEGAL_SUFFIXES]
        return tokens

    def _normalize(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def _build_config(self, company_name: str, board_token: str) -> dict:
        return {
            "company_name": company_name,
            "company_slug": board_token,
            "board_token": board_token,
            "base_url": f"{BOARD_API_ROOT}/{board_token}/jobs",
            "ats": "greenhouse",
        }

    def _verify_board(self, config: dict) -> bool:
        try:
            resp = self._session.get(
                config["base_url"],
                params={"content": "true"},
                timeout=10,
            )
            if resp.status_code != 200:
                return False

            data = resp.json()
            jobs = data.get("jobs", [])
            return isinstance(jobs, list)
        except requests.RequestException:
            return False

    def fetch_jobs(
        self,
        company_config: dict,
        recency_days: int = 30,
        location_prefs: list[str] | None = None,
    ) -> tuple[list[dict], dict]:
        """Fetch job listings from Greenhouse's Job Board API and apply filters."""
        api_url = company_config["base_url"]
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=recency_days)

        logger.info(
            f"Fetching jobs from {company_config.get('company_name', company_config.get('board_token', ''))} "
            f"via Greenhouse (recency={recency_days}d, locations={location_prefs})..."
        )

        try:
            resp = self._session.get(
                api_url,
                params={"content": "true"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error(f"Greenhouse API request failed: {exc}")
            return [], {
                "total_scanned": 0,
                "skipped_recency": 0,
                "skipped_location": 0,
                "matched": 0,
            }

        postings = data.get("jobs", [])
        total = data.get("meta", {}).get("total", len(postings))
        logger.info(f"  Scanning {len(postings)}/{total} listings...")

        all_jobs = []
        skipped_recency = 0
        skipped_location = 0

        for posting in postings:
            job, skip_reason = self._process_posting(
                posting,
                company_config,
                cutoff_date,
                location_prefs,
            )
            if job:
                all_jobs.append(job)
            elif skip_reason == "recency":
                skipped_recency += 1
            elif skip_reason == "location":
                skipped_location += 1

        filter_stats = {
            "total_scanned": len(postings),
            "skipped_recency": skipped_recency,
            "skipped_location": skipped_location,
            "matched": len(all_jobs),
        }

        logger.info(
            f"\n  Filter Summary for '{company_config.get('company_name', company_config.get('board_token', ''))}':\n"
            f"     Total scanned:     {len(postings)}\n"
            f"     Skipped (recency): {skipped_recency}\n"
            f"     Skipped (location):{skipped_location}\n"
            f"     Matched:           {len(all_jobs)}"
        )
        return all_jobs, filter_stats

    def _process_posting(
        self,
        posting: dict,
        company_config: dict,
        cutoff_date: datetime,
        location_prefs: list[str] | None,
    ) -> tuple[dict | None, str]:
        title = posting.get("title", "Unknown")
        job_id = posting.get("id", "")
        updated_at = posting.get("updated_at", "")
        location = posting.get("location", {}).get("name", "")

        if updated_at:
            try:
                post_date = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if post_date < cutoff_date:
                    return None, "recency"
            except (ValueError, TypeError):
                pass

        if location_prefs and location:
            loc_lower = location.lower()
            if not any(pref.lower() in loc_lower for pref in location_prefs):
                return None, "location"

        description = BeautifulSoup(
            posting.get("content", "") or "",
            "html.parser",
        ).get_text(separator="\n").strip()

        return {
            "title": title,
            "job_req_id": str(job_id),
            "url": posting.get("absolute_url", ""),
            "location": location,
            "posted_date": updated_at[:10] if updated_at else "",
            "description": description,
            "company": company_config.get(
                "company_name",
                company_config.get("board_token", ""),
            ),
        }, ""
