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
PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "candidate_profile.json"


def load_config(config_path: str | None = None) -> dict:
    """Load and validate the configuration file."""
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Validate required fields
    required = ["companies"]
    for field in required:
        if field not in config:
            raise ValueError(f"Missing required config field: '{field}'")

    model_from_env = os.getenv("OPENROUTER_MODEL", "").strip()
    premium_model = os.getenv("OPENROUTER_PREMIUM_MODEL", "").strip()
    fast_model = os.getenv("OPENROUTER_FAST_MODEL", "").strip()
    qa_model = os.getenv("OPENROUTER_QA_MODEL", "").strip()

    resolved_base_model = premium_model or model_from_env or str(config.get("model", "")).strip()
    if resolved_base_model:
        config["model"] = resolved_base_model
    else:
        raise ValueError(
            "Missing model configuration. Set OPENROUTER_MODEL in the environment "
            "or provide 'model' in config.json."
        )
    config["premium_model"] = resolved_base_model
    config["fast_provider"] = get_fast_provider()
    if config["fast_provider"] == "typesafe":
        typesafe_model = os.getenv("TYPESAFE_MODEL", "").strip()
        config["fast_model"] = typesafe_model or "jev-latest"
    else:
        config["fast_model"] = fast_model or resolved_base_model
    config["qa_model"] = qa_model or resolved_base_model

    # Defaults
    config.setdefault("recency_days", 30)
    config.setdefault("candidate_profile_path", str(PROFILE_PATH))

    return config


def get_fast_provider() -> str:
    """Resolve the stage-1 triage provider from the FAST_PROVIDER env var."""
    provider = os.getenv("FAST_PROVIDER", "").strip().lower()
    return "typesafe" if provider in {"typesafe", "type_safe", "jev"} else "openrouter"


def load_api_key(kind: str = "premium") -> str:
    """Load the appropriate LLM API key from environment."""
    if kind == "fast" and get_fast_provider() == "typesafe":
        key = os.getenv("TYPESAFE_API_KEY", "").strip()
        if not key:
            raise EnvironmentError(
                "FAST_PROVIDER=typesafe but TYPESAFE_API_KEY is not set. "
                "Add it to the .env file or environment."
            )
        return key
    env_names = {
        "fast": ["OPENROUTER_FAST_API_KEY", "OPENROUTER_API_KEY"],
        "premium": ["OPENROUTER_PREMIUM_API_KEY", "OPENROUTER_API_KEY"],
        "qa": ["OPENROUTER_QA_API_KEY", "OPENROUTER_API_KEY"],
    }
    key = ""
    for env_name in env_names.get(kind, ["OPENROUTER_API_KEY"]):
        candidate = os.getenv(env_name, "").strip()
        if candidate:
            key = candidate
            break
    if not key:
        raise EnvironmentError(
            "OpenRouter API key not set. Create a .env file or set the environment variable."
        )
    return key
