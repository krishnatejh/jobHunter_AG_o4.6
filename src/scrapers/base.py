"""Abstract base class for job scrapers."""

from abc import ABC, abstractmethod


class BaseScraper(ABC):
    """Base interface for all job board scrapers."""

    @abstractmethod
    def discover(self, company_name: str) -> dict:
        """Discover the company's careers page API details.

        Args:
            company_name: Human-readable company name (e.g., "Mastercard").

        Returns:
            Dict with platform-specific connection details.
        """
        ...

    @abstractmethod
    def fetch_jobs(
        self,
        company_config: dict,
        recency_days: int = 30,
        location_prefs: list[str] | None = None,
    ) -> list[dict]:
        """Fetch and filter job listings.

        Args:
            company_config: Connection details from discover().
            recency_days: Only return jobs posted within this many days.
            location_prefs: Preferred locations to filter by.

        Returns:
            List of job dicts with keys: title, url, location, posted_date, description.
        """
        ...
