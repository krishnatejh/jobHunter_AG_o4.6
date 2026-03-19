"""SQLite-based job history cache — avoids re-scoring already-analyzed jobs."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "job_history.db"

# ------------------------------------------------------------------ #
#  Schema
# ------------------------------------------------------------------ #
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS job_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company       TEXT NOT NULL,
    job_key       TEXT NOT NULL,
    job_req_id    TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    posted_date   TEXT DEFAULT '',
    end_date      TEXT DEFAULT '',
    time_left     TEXT DEFAULT '',
    time_type     TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    score         INTEGER DEFAULT 0,
    strengths     TEXT DEFAULT '[]',
    gaps          TEXT DEFAULT '[]',
    verdict       TEXT DEFAULT '',
    thinking      TEXT DEFAULT '',
    model_used    TEXT DEFAULT '',
    first_seen    TEXT NOT NULL,
    analyzed_at   TEXT NOT NULL,
    UNIQUE(company, job_key)
);
"""


class JobCache:
    """Manages a local SQLite cache of previously analyzed jobs.

    Lookup logic:
        1. Compute a stable job_key for each scraped job.
        2. If (company, job_key) exists AND posted_date matches → CACHED.
        3. If (company, job_key) exists BUT posted_date differs → REPOSTED.
        4. If not found → NEW.
    """

    def __init__(self, db_path: Path | str | None = None):
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()
        logger.info(f"Job cache: {self._db_path}")

    # ---------------------------------------------------------------- #
    #  Key generation
    # ---------------------------------------------------------------- #
    @staticmethod
    def make_job_key(job: dict) -> str:
        """Generate a stable, unique key for a job posting.

        Tiered priority:
            1. job_req_id  (e.g. "R-268272") — most reliable
            2. URL path    (unique per posting on any ATS)
            3. hash(company + title + location) — last resort
        """
        # Priority 1: job requisition ID
        req_id = job.get("job_req_id", "").strip()
        if req_id:
            return f"req:{req_id}"

        # Priority 2: URL (strip domain, keep path)
        url = job.get("url", "").strip()
        if url:
            # Extract the path portion after the domain
            from urllib.parse import urlparse
            path = urlparse(url).path
            if path and path != "/":
                return f"url:{path}"

        # Priority 3: content hash
        raw = f"{job.get('company', '')}|{job.get('title', '')}|{job.get('location', '')}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"hash:{h}"

    # ---------------------------------------------------------------- #
    #  Lookup
    # ---------------------------------------------------------------- #
    def lookup(self, company: str, job_key: str, posted_date: str) -> tuple[dict | None, str]:
        """Check if a job is already cached.

        Args:
            company: Company name.
            job_key: Stable key from make_job_key().
            posted_date: Current posted date from the scraper.

        Returns:
            Tuple of (cached_analysis_or_None, status).
            status is one of: 'new', 'cached', 'reposted'.
        """
        row = self._conn.execute(
            "SELECT * FROM job_history WHERE company = ? AND job_key = ?",
            (company, job_key),
        ).fetchone()

        if row is None:
            return None, "new"

        # Check if the job was reposted (different posted_date)
        cached_date = row["posted_date"] or ""
        current_date = posted_date or ""

        if cached_date and current_date and cached_date != current_date:
            logger.info(f"    Reposted: {cached_date} → {current_date}")
            return None, "reposted"

        # Return cached analysis
        analysis = {
            "score": row["score"],
            "strengths": json.loads(row["strengths"]),
            "gaps": json.loads(row["gaps"]),
            "verdict": row["verdict"],
            "thinking": row["thinking"],
            "model_used": row["model_used"],
        }
        return analysis, "cached"

    # ---------------------------------------------------------------- #
    #  Save
    # ---------------------------------------------------------------- #
    def save(self, job: dict, job_key: str, analysis: dict) -> None:
        """Save or update a job's analysis in the cache."""
        now = datetime.now().isoformat(timespec="seconds")
        company = job.get("company", "")

        # Check if row exists (for first_seen preservation)
        existing = self._conn.execute(
            "SELECT first_seen FROM job_history WHERE company = ? AND job_key = ?",
            (company, job_key),
        ).fetchone()

        first_seen = existing["first_seen"] if existing else now

        self._conn.execute(
            """
            INSERT INTO job_history
                (company, job_key, job_req_id, title, location, url,
                 posted_date, end_date, time_left, time_type, description,
                 score, strengths, gaps, verdict, thinking, model_used,
                 first_seen, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, job_key) DO UPDATE SET
                title = excluded.title,
                location = excluded.location,
                url = excluded.url,
                posted_date = excluded.posted_date,
                end_date = excluded.end_date,
                time_left = excluded.time_left,
                time_type = excluded.time_type,
                description = excluded.description,
                score = excluded.score,
                strengths = excluded.strengths,
                gaps = excluded.gaps,
                verdict = excluded.verdict,
                thinking = excluded.thinking,
                model_used = excluded.model_used,
                analyzed_at = excluded.analyzed_at
            """,
            (
                company,
                job_key,
                job.get("job_req_id", ""),
                job.get("title", ""),
                job.get("location", ""),
                job.get("url", ""),
                job.get("posted_date", ""),
                job.get("end_date", ""),
                job.get("time_left", ""),
                job.get("time_type", ""),
                job.get("description", "")[:2000],  # Truncate for storage
                analysis.get("score", 0),
                json.dumps(analysis.get("strengths", [])),
                json.dumps(analysis.get("gaps", [])),
                analysis.get("verdict", ""),
                analysis.get("thinking", "")[:3000],  # Truncate thinking
                analysis.get("model_used", ""),
                first_seen,
                now,
            ),
        )
        self._conn.commit()

    # ---------------------------------------------------------------- #
    #  Stats
    # ---------------------------------------------------------------- #
    def get_stats(self) -> dict:
        """Get summary statistics from the cache."""
        row = self._conn.execute(
            "SELECT COUNT(*) as total, "
            "AVG(score) as avg_score, "
            "MAX(analyzed_at) as last_run "
            "FROM job_history"
        ).fetchone()
        return {
            "total_cached": row["total"],
            "avg_score": round(row["avg_score"] or 0),
            "last_run": row["last_run"] or "never",
        }

    def close(self):
        """Close the database connection."""
        self._conn.close()
