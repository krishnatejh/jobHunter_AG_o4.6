"""Configuration loader — reads config.json and environment variables."""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (gracefully handle missing or badly-encoded files)
_env_path = Path(__file__).resolve().parent.parent / ".env"
try:
    load_dotenv(_env_path)
except Exception:
    # PowerShell's echo creates UTF-16 files that python-dotenv can't parse.
    # Try reading manually as a fallback.
    try:
        text = _env_path.read_text(encoding="utf-8-sig")  # handles BOM
    except (UnicodeDecodeError, FileNotFoundError):
        try:
            text = _env_path.read_text(encoding="utf-16")
        except (UnicodeDecodeError, FileNotFoundError):
            text = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_config(config_path: str | None = None) -> dict:
    """Load and validate the configuration file."""
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Validate required fields
    required = ["model", "companies"]
    for field in required:
        if field not in config:
            raise ValueError(f"Missing required config field: '{field}'")

    # Defaults
    config.setdefault("location_preferences", [])
    config.setdefault("recency_days", 30)
    config.setdefault("candidate_preferences", {})

    return config


def load_api_key() -> str:
    """Load the OpenRouter API key from environment."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise EnvironmentError(
            "OPENROUTER_API_KEY not set. Create a .env file or set the environment variable."
        )
    return key
