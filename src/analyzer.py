"""LLM-based job-resume analyzer — scores job fit via OpenRouter API."""

import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are a senior technical recruiter and career advisor with deep expertise in matching candidate profiles to job opportunities.

Your task is to analyze how well a candidate's resume matches a specific job posting. You must think step-by-step through the evaluation before arriving at a score.

Evaluate the match across these dimensions:
1. **Technical Skills Match** — Do the candidate's skills align with the job requirements?
2. **Experience Level** — Does seniority/years of experience match?
3. **Domain Knowledge** — Does the candidate have relevant industry experience?
4. **Education** — Does education meet the requirements?
5. **Location Fit** — Is the candidate's location compatible?

After thorough analysis, return your response as a JSON object with exactly these keys:
{
  "score": <integer 0-100>,
  "strengths": ["strength1", "strength2", ...],
  "gaps": ["gap1", "gap2", ...],
  "verdict": "<one paragraph summary of fit>"
}

Scoring guide:
- 85-100: Excellent match — candidate is a strong fit
- 70-84: Good match — most requirements met
- 50-69: Moderate match — some gaps but transferable skills
- 30-49: Weak match — significant gaps
- 0-29: Poor match — not suitable

Return ONLY the JSON object, no additional text."""


def score_job(
    resume_text: str,
    job: dict,
    model: str,
    api_key: str,
    max_retries: int = 3,
) -> dict:
    """Score how well a resume matches a job posting using an LLM.

    Args:
        resume_text: Full text of the candidate's resume.
        job: Job dict with title, description, location, etc.
        model: OpenRouter model identifier (e.g., "deepseek/deepseek-r1").
        api_key: OpenRouter API key.
        max_retries: Number of retry attempts for transient errors.

    Returns:
        Dict with score, strengths, gaps, verdict, and thinking.
    """
    # Truncate very long descriptions to avoid token limits / 500 errors
    description = job.get("description", "No description available.")
    if len(description) > 6000:
        description = description[:6000] + "\n\n[... truncated for length]"

    user_prompt = f"""## Candidate Resume
{resume_text}

---

## Job Posting
**Title:** {job.get('title', 'N/A')}
**Location:** {job.get('location', 'N/A')}
**Company:** {job.get('company', 'N/A')}

**Description:**
{description}

---

Analyze the fit between this candidate and this job. Think carefully through each dimension before scoring."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jobhunter-agent",
        "X-Title": "Job Hunter Agent",
    }

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"  Scoring: {job.get('title', '?')} @ {job.get('company', '?')}"
                       + (f" (attempt {attempt})" if attempt > 1 else ""))

            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            # Extract the response content
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "")

            # Some models (like deepseek-r1) return thinking in a separate field
            thinking = message.get("reasoning", "") or message.get("reasoning_content", "")

            # Parse JSON from the LLM response
            result = _parse_llm_response(content)
            result["thinking"] = thinking
            result["model_used"] = model

            return result

        except requests.RequestException as e:
            last_error = e
            status_code = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            logger.warning(f"  Attempt {attempt}/{max_retries} failed: {e}")

            # Retry on 500, 502, 503, 429 (rate limit)
            if status_code in (500, 502, 503, 429) and attempt < max_retries:
                wait = 2 ** attempt  # Exponential backoff: 2s, 4s
                logger.info(f"  Retrying in {wait}s...")
                import time
                time.sleep(wait)
                continue

            # Non-retryable error or all retries exhausted
            break

    logger.error(f"  OpenRouter API error after {max_retries} attempts: {last_error}")
    return {
        "score": 0,
        "strengths": [],
        "gaps": ["API call failed"],
        "verdict": f"Error: {str(last_error)}",
        "thinking": "",
        "model_used": model,
    }


def _parse_llm_response(content: str) -> dict:
    """Parse the JSON response from the LLM, handling markdown code blocks."""
    # Strip markdown code fences if present
    content = content.strip()
    if content.startswith("```"):
        # Remove ```json ... ``` wrapper
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

    try:
        result = json.loads(content)
        # Validate expected keys
        return {
            "score": int(result.get("score", 0)),
            "strengths": result.get("strengths", []),
            "gaps": result.get("gaps", []),
            "verdict": result.get("verdict", ""),
        }
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"  Failed to parse LLM response as JSON: {e}")
        # Fallback: try to extract score from text
        score_match = re.search(r'"score"\s*:\s*(\d+)', content)
        score = int(score_match.group(1)) if score_match else 0
        return {
            "score": score,
            "strengths": [],
            "gaps": ["Could not parse structured response"],
            "verdict": content[:500],
        }
