"""GitHub Actions entrypoint for Job Hunter automation."""

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import print_summary, run_pipeline
from src.telegram_notifier import (
    format_failure_message,
    format_summary_message,
    send_document,
    send_message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("automation")

DEFAULT_SUMMARY_PATH = ROOT / "output" / "last_run_summary.json"
DEFAULT_LATEST_REPORT = ROOT / "output" / "latest_report.html"


def build_parser() -> argparse.ArgumentParser:
    """Build the automation CLI parser."""
    parser = argparse.ArgumentParser(description="Run Job Hunter in automation mode.")
    parser.add_argument(
        "--resume",
        default="resume.pdf",
        help="Path to the resume PDF. Defaults to resume.pdf in the repo root.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional config path. Defaults to config.json in the repo root.",
    )
    parser.add_argument(
        "--force-rescore",
        action="store_true",
        help="Ignore cache and re-score every job.",
    )
    parser.add_argument(
        "--company",
        action="append",
        dest="company_filters",
        default=None,
        help="Only run for the specified company name. Repeat to target multiple companies.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(DEFAULT_SUMMARY_PATH),
        help="Where to write the structured JSON summary.",
    )
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Run the pipeline but do not send Telegram notifications.",
    )
    return parser


def ensure_latest_report(report_path: str) -> str:
    """Copy the latest report to a stable filename for artifact packaging."""
    source = Path(report_path)
    DEFAULT_LATEST_REPORT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, DEFAULT_LATEST_REPORT)
    return str(DEFAULT_LATEST_REPORT)


def write_summary(summary: dict, summary_path: str) -> None:
    """Persist the structured summary to disk."""
    path = Path(summary_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def maybe_send_telegram(summary: dict) -> None:
    """Send summary and report to Telegram if secrets are present."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise EnvironmentError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set for Telegram delivery."
        )

    message = format_summary_message(summary)
    send_message(bot_token, chat_id, message)
    send_document(
        bot_token,
        chat_id,
        summary["report_path"],
        caption="Job Hunter report",
    )


def send_failure_telegram(stage: str, error: str) -> None:
    """Best-effort failure notification."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.warning("Telegram credentials missing; skipping failure notification.")
        return

    try:
        send_message(bot_token, chat_id, format_failure_message(stage, error))
    except Exception as exc:
        logger.warning(f"Failed to send Telegram failure message: {exc}")


def main() -> int:
    """Run the pipeline and send notifications."""
    args = build_parser().parse_args()

    try:
        summary = run_pipeline(
            resume_path=args.resume,
            config_path=args.config,
            force_rescore=args.force_rescore,
            company_filters=args.company_filters,
        )
        summary["report_path"] = ensure_latest_report(summary["report_path"])
        summary["summary_path"] = str(Path(args.summary_json).resolve())
        write_summary(summary, args.summary_json)
        print_summary(summary)

        if not args.skip_telegram:
            maybe_send_telegram(summary)
        else:
            logger.info("Telegram notification skipped by flag.")

        return 0
    except Exception as exc:
        logger.exception("Automation run failed.")
        send_failure_telegram("pipeline", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
