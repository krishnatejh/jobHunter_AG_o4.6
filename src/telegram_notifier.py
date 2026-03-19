"""Telegram notification helpers for automation runs."""

import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def format_summary_message(summary: dict) -> str:
    """Format a Telegram-friendly success message."""
    sections = summary.get("sections", {})
    top_matches = summary.get("top_matches", [])

    lines = [
        "Job Hunter run completed successfully.",
        "",
        f"Companies scanned: {summary.get('companies_scanned', 0)}",
        f"Jobs found: {summary.get('jobs_found', 0)}",
        f"New Findings: {sections.get('new_findings', 0)}",
        f"Cached Findings: {sections.get('cached_findings', 0)}",
        f"Borderline: {sections.get('borderline', 0)}",
        f"Waitlist: {sections.get('waitlist', 0)}",
        f"New analyzed: {summary.get('scored_new', 0)}",
        f"Cached reused: {summary.get('scored_cached', 0)}",
        f"Reposted rescored: {summary.get('scored_reposted', 0)}",
        f"Failed: {summary.get('scored_failed', 0)}",
    ]

    if summary.get("jobs_found"):
        lines.extend([
            f"Highest score: {summary.get('highest_score', 0)}/100",
            f"Average score: {summary.get('average_score', 0)}/100",
        ])

    if top_matches:
        lines.append("")
        lines.append("Top matches:")
        for match in top_matches[:5]:
            lines.append(
                f"- {match.get('score', 0)}/100 | {match.get('title', '')} @ "
                f"{match.get('company', '')}"
            )

    return "\n".join(lines)


def format_failure_message(stage: str, error: str) -> str:
    """Format a Telegram-friendly failure alert."""
    return (
        "Job Hunter run failed.\n\n"
        f"Stage: {stage}\n"
        f"Error: {error[:1500]}"
    )


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    """Send a plain Telegram message."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    resp.raise_for_status()


def send_document(
    bot_token: str,
    chat_id: str,
    file_path: str | Path,
    caption: str = "",
) -> None:
    """Send a document attachment to Telegram."""
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    path = Path(file_path)
    with path.open("rb") as file_handle:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (path.name, file_handle, "text/html")},
            timeout=120,
        )
    resp.raise_for_status()
    logger.info(f"Telegram document sent: {path}")
