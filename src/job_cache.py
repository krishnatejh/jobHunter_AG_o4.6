"""SQLite-based job history cache -- avoids re-scoring already-analyzed jobs."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "job_history.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS job_history (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    company                     TEXT NOT NULL,
    job_key                     TEXT NOT NULL,
    job_req_id                  TEXT DEFAULT '',
    title                       TEXT DEFAULT '',
    location                    TEXT DEFAULT '',
    url                         TEXT DEFAULT '',
    posted_date                 TEXT DEFAULT '',
    end_date                    TEXT DEFAULT '',
    time_left                   TEXT DEFAULT '',
    time_type                   TEXT DEFAULT '',
    description                 TEXT DEFAULT '',
    prepared_job_description    TEXT DEFAULT '',
    score                       INTEGER DEFAULT 0,
    component_scores            TEXT DEFAULT '{}',
    hard_requirements_met       TEXT DEFAULT '[]',
    hard_requirements_missing   TEXT DEFAULT '[]',
    nice_to_have_matches        TEXT DEFAULT '[]',
    role_family_match           TEXT DEFAULT '',
    seniority_fit               TEXT DEFAULT '',
    location_signal             TEXT DEFAULT '',
    location_preference_applied INTEGER DEFAULT 0,
    parse_error                 TEXT DEFAULT '',
    strengths                   TEXT DEFAULT '[]',
    gaps                        TEXT DEFAULT '[]',
    verdict                     TEXT DEFAULT '',
    thinking                    TEXT DEFAULT '',
    raw_system_prompt           TEXT DEFAULT '',
    raw_user_prompt             TEXT DEFAULT '',
    raw_llm_content             TEXT DEFAULT '',
    raw_llm_reasoning           TEXT DEFAULT '',
    model_used                  TEXT DEFAULT '',
    scoring_version             TEXT DEFAULT '',
    first_seen                  TEXT NOT NULL,
    analyzed_at                 TEXT NOT NULL,
    UNIQUE(company, job_key)
);
"""

_COLUMN_DEFAULTS = {
    "scoring_version": "TEXT DEFAULT ''",
    "prepared_job_description": "TEXT DEFAULT ''",
    "component_scores": "TEXT DEFAULT '{}'",
    "hard_requirements_met": "TEXT DEFAULT '[]'",
    "hard_requirements_missing": "TEXT DEFAULT '[]'",
    "nice_to_have_matches": "TEXT DEFAULT '[]'",
    "role_family_match": "TEXT DEFAULT ''",
    "seniority_fit": "TEXT DEFAULT ''",
    "location_signal": "TEXT DEFAULT ''",
    "location_preference_applied": "INTEGER DEFAULT 0",
    "parse_error": "TEXT DEFAULT ''",
    "raw_system_prompt": "TEXT DEFAULT ''",
    "raw_user_prompt": "TEXT DEFAULT ''",
    "raw_llm_content": "TEXT DEFAULT ''",
    "raw_llm_reasoning": "TEXT DEFAULT ''",
}


def _json_loads(value: str, default):
    """Parse JSON strings from SQLite safely."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class JobCache:
    """Manages a local SQLite cache of previously analyzed jobs."""

    def __init__(self, db_path: Path | str | None = None):
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._ensure_columns()
        self._conn.commit()
        logger.info(f"Job cache: {self._db_path}")

    def _ensure_columns(self) -> None:
        """Backfill columns added after the initial schema."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(job_history)").fetchall()
        }
        for column, ddl in _COLUMN_DEFAULTS.items():
            if column not in columns:
                self._conn.execute(f"ALTER TABLE job_history ADD COLUMN {column} {ddl}")
        self._conn.commit()

    @staticmethod
    def make_job_key(job: dict) -> str:
        """Generate a stable, unique key for a job posting."""
        req_id = job.get("job_req_id", "").strip()
        if req_id:
            return f"req:{req_id}"

        url = job.get("url", "").strip()
        if url:
            from urllib.parse import urlparse

            path = urlparse(url).path
            if path and path != "/":
                return f"url:{path}"

        raw = f"{job.get('company', '')}|{job.get('title', '')}|{job.get('location', '')}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"hash:{digest}"

    def _row_to_analysis(self, row: sqlite3.Row) -> dict:
        """Hydrate a cached analysis row into the runtime analysis shape."""
        return {
            "score": row["score"],
            "component_scores": _json_loads(row["component_scores"], {}),
            "hard_requirements_met": _json_loads(row["hard_requirements_met"], []),
            "hard_requirements_missing": _json_loads(row["hard_requirements_missing"], []),
            "nice_to_have_matches": _json_loads(row["nice_to_have_matches"], []),
            "role_family_match": row["role_family_match"] or "adjacent",
            "seniority_fit": row["seniority_fit"] or "matched",
            "location_signal": row["location_signal"] or "unknown",
            "location_preference_applied": bool(row["location_preference_applied"]),
            "parse_error": row["parse_error"] or "",
            "strengths": _json_loads(row["strengths"], []),
            "gaps": _json_loads(row["gaps"], []),
            "verdict": row["verdict"],
            "thinking": row["thinking"],
            "raw_system_prompt": row["raw_system_prompt"] or "",
            "raw_user_prompt": row["raw_user_prompt"] or "",
            "raw_llm_content": row["raw_llm_content"] or "",
            "raw_llm_reasoning": row["raw_llm_reasoning"] or "",
            "prepared_job_description": row["prepared_job_description"] or "",
            "model_used": row["model_used"],
            "scoring_version": row["scoring_version"] or "",
        }

    def _is_legacy_analysis(self, row: sqlite3.Row) -> bool:
        """Detect rows missing the structured fields required by the new scorer."""
        return any(
            not (row[column] or "").strip()
            for column in (
                "component_scores",
                "hard_requirements_missing",
                "role_family_match",
                "seniority_fit",
                "location_signal",
                "prepared_job_description",
                "raw_system_prompt",
                "raw_user_prompt",
                "raw_llm_content",
            )
        )

    def lookup(
        self,
        company: str,
        job_key: str,
        posted_date: str,
        scoring_version: str = "",
    ) -> tuple[dict | None, str]:
        """Check if a job is already cached."""
        row = self._conn.execute(
            "SELECT * FROM job_history WHERE company = ? AND job_key = ?",
            (company, job_key),
        ).fetchone()

        if row is None:
            return None, "new"

        cached_date = row["posted_date"] or ""
        current_date = posted_date or ""
        if cached_date and current_date and cached_date != current_date:
            logger.info(f"    Reposted: {cached_date} -> {current_date}")
            return None, "reposted"

        cached_version = row["scoring_version"] or ""
        if scoring_version and cached_version != scoring_version:
            logger.info(
                "    Re-scoring due to scoring version change: "
                f"{cached_version or 'unset'} -> {scoring_version}"
            )
            return None, "reposted"

        if self._is_legacy_analysis(row):
            logger.info("    Re-scoring due to incomplete cached structured analysis")
            return None, "reposted"

        return self._row_to_analysis(row), "cached"

    def save(self, job: dict, job_key: str, analysis: dict) -> None:
        """Save or update a job's analysis in the cache."""
        now = datetime.now().isoformat(timespec="seconds")
        company = job.get("company", "")

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
                 prepared_job_description, score, component_scores,
                 hard_requirements_met, hard_requirements_missing, nice_to_have_matches,
                 role_family_match, seniority_fit, location_signal,
                 location_preference_applied, parse_error, strengths, gaps, verdict,
                 thinking, raw_system_prompt, raw_user_prompt, raw_llm_content,
                 raw_llm_reasoning, model_used, scoring_version, first_seen, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, job_key) DO UPDATE SET
                title = excluded.title,
                location = excluded.location,
                url = excluded.url,
                posted_date = excluded.posted_date,
                end_date = excluded.end_date,
                time_left = excluded.time_left,
                time_type = excluded.time_type,
                description = excluded.description,
                prepared_job_description = excluded.prepared_job_description,
                score = excluded.score,
                component_scores = excluded.component_scores,
                hard_requirements_met = excluded.hard_requirements_met,
                hard_requirements_missing = excluded.hard_requirements_missing,
                nice_to_have_matches = excluded.nice_to_have_matches,
                role_family_match = excluded.role_family_match,
                seniority_fit = excluded.seniority_fit,
                location_signal = excluded.location_signal,
                location_preference_applied = excluded.location_preference_applied,
                parse_error = excluded.parse_error,
                strengths = excluded.strengths,
                gaps = excluded.gaps,
                verdict = excluded.verdict,
                thinking = excluded.thinking,
                raw_system_prompt = excluded.raw_system_prompt,
                raw_user_prompt = excluded.raw_user_prompt,
                raw_llm_content = excluded.raw_llm_content,
                raw_llm_reasoning = excluded.raw_llm_reasoning,
                model_used = excluded.model_used,
                scoring_version = excluded.scoring_version,
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
                job.get("description", ""),
                analysis.get("prepared_job_description", ""),
                analysis.get("score", 0),
                json.dumps(analysis.get("component_scores", {}), ensure_ascii=False),
                json.dumps(analysis.get("hard_requirements_met", []), ensure_ascii=False),
                json.dumps(analysis.get("hard_requirements_missing", []), ensure_ascii=False),
                json.dumps(analysis.get("nice_to_have_matches", []), ensure_ascii=False),
                analysis.get("role_family_match", ""),
                analysis.get("seniority_fit", ""),
                analysis.get("location_signal", ""),
                int(bool(analysis.get("location_preference_applied"))),
                analysis.get("parse_error", ""),
                json.dumps(analysis.get("strengths", []), ensure_ascii=False),
                json.dumps(analysis.get("gaps", []), ensure_ascii=False),
                analysis.get("verdict", ""),
                analysis.get("thinking", ""),
                analysis.get("raw_system_prompt", ""),
                analysis.get("raw_user_prompt", ""),
                analysis.get("raw_llm_content", ""),
                analysis.get("raw_llm_reasoning", ""),
                analysis.get("model_used", ""),
                analysis.get("scoring_version", ""),
                first_seen,
                now,
            ),
        )
        self._conn.commit()

    def get_job_debug(self, job_req_id: str) -> dict | None:
        """Fetch a full debug payload for a specific job id."""
        row = self._conn.execute(
            "SELECT * FROM job_history WHERE job_req_id = ? ORDER BY analyzed_at DESC LIMIT 1",
            (job_req_id,),
        ).fetchone()
        if row is None:
            return None

        analysis = self._row_to_analysis(row)
        return {
            "job": {
                "company": row["company"],
                "job_key": row["job_key"],
                "job_req_id": row["job_req_id"],
                "title": row["title"],
                "location": row["location"],
                "url": row["url"],
                "posted_date": row["posted_date"],
                "end_date": row["end_date"],
                "time_left": row["time_left"],
                "time_type": row["time_type"],
                "description": row["description"],
                "prepared_job_description": row["prepared_job_description"],
                "first_seen": row["first_seen"],
                "analyzed_at": row["analyzed_at"],
            },
            "analysis": analysis,
            "raw_prompts": {
                "system": row["raw_system_prompt"] or "",
                "user": row["raw_user_prompt"] or "",
            },
            "raw_model_output": {
                "content": row["raw_llm_content"] or "",
                "reasoning": row["raw_llm_reasoning"] or "",
            },
        }

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
