# Job Hunter Agent

An automated job discovery and matching pipeline that scrapes supported ATS boards, filters jobs by recency and location, screens and scores them against a candidate profile, caches results, and produces an interactive HTML report.

## What It Does

- supports multiple ATS sources:
  - Workday
  - SmartRecruiters
  - Greenhouse
- filters jobs before LLM calls using:
  - `recency_days`
  - deterministic title filtering
- builds and uses a sanitized `candidate_profile.json`
- runs a two-stage evaluation flow:
  - Stage 1: fast LLM triage (`Pass`, `Borderline`, `Reject`)
  - Stage 2: premium LLM scoring for jobs that survive stage 1
- caches results in SQLite to avoid repeated work
- generates an HTML report with four sections:
  - `New Findings`
  - `Cached Findings`
  - `Borderline`
  - `Waitlist`
- includes a compact `Screen` indicator in the report
- supports GitHub Actions runs and Telegram delivery
- supports Telegram delivery for local runs too

## Current Matching Model

The current matcher is candidate-profile-based, prompt-auditable, and cache-versioned.

Implemented today and currently active:
- sanitized candidate profile generation and loading
- separate stage-1 screening prompt and stage-2 scoring prompt
- deterministic title pre-filtering before LLM calls
- stage-1 screen routing with `Pass`, `Borderline`, and `Reject`
- structured JSON scoring from the premium model
- parse-failure retry with JSON repair
- preferred-location handling from `candidate_profile.json`
- stage-level audit storage for prompts and raw API responses
- compact `Screen` indicator in the report

Prepared but not yet active:
- QA stage prompt/config/cache shape

Current report buckets:
- `New Findings`: new/reposted jobs with score `>= 70`
- `Cached Findings`: cached jobs with score `>= 70`
- `Borderline`: jobs with score `50-69`
- `Waitlist`: jobs with score `< 50`

## Setup

```powershell
python -m venv venv
.\venv\Scripts\activate
python -m pip install -r requirements.txt
```

Create `.env` with:

```text
OPENROUTER_API_KEY=your_openrouter_api_key_here
OPENROUTER_FAST_MODEL=nvidia/nemotron-3-super-120b-a12b:free
OPENROUTER_PREMIUM_MODEL=nvidia/nemotron-3-super-120b-a12b:free
OPENROUTER_QA_MODEL=nvidia/nemotron-3-super-120b-a12b:free
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

Optional stage-specific API keys:

```text
OPENROUTER_FAST_API_KEY=your_fast_model_key
OPENROUTER_PREMIUM_API_KEY=your_premium_model_key
OPENROUTER_QA_API_KEY=your_qa_model_key
```

Optional: use TypeSafe Jev for the stage-1 fast triage instead of OpenRouter:

```text
FAST_PROVIDER=typesafe
TYPESAFE_API_KEY=your_typesafe_api_key_here
TYPESAFE_MODEL=jev-latest
```

Notes:
- if you do not set stage-specific keys, all stages fall back to `OPENROUTER_API_KEY`
- when `FAST_PROVIDER=typesafe`, the fast screener calls the TypeSafe System One API directly and bills your Typesafe credits; stage 2 (premium scoring) still uses OpenRouter
- when `FAST_PROVIDER=typesafe`, the fast model defaults to `jev-latest` unless `TYPESAFE_MODEL` is set (`OPENROUTER_FAST_MODEL` is ignored in this mode)
- `OPENROUTER_QA_MODEL` can be configured now, but QA review is not yet active in the runtime pipeline
- `.env` files created by PowerShell may be UTF-16; the loader includes a fallback for that

## Onboarding Companies

Use `onboard.py` to discover and save company ATS endpoints.

```powershell
python onboard.py --company "Mastercard"
python onboard.py --company "Razorpay" --ats greenhouse --refresh
python onboard.py --list
```

## Candidate Profile Workflow

The runtime no longer relies only on raw resume text.

It generates and uses a local-only profile:
- `data/candidate_profile.json` (ignored by git; never commit the real one)
- [candidate_profile.example.json](./data/candidate_profile.example.json) (synthetic example committed for reference)

Keep your real `resume.pdf` and `data/candidate_profile.json` out of git; they are personal data. Fresh clones start from the synthetic example and regenerate with `--resume resume.pdf --refresh-candidate-profile`.

This profile is the primary LLM input layer and contains:
- short and long candidate summaries
- target roles
- avoid roles
- preferred locations
- preferred seniority
- evidence highlights
- not-directly-evidenced areas
- redacted resume context for premium scoring

Important behavior:
- the premium scorer uses the richer candidate profile view
- the fast screener uses a compact candidate profile view
- if you manually fine-tune `candidate_profile.json`, do not use `--refresh-candidate-profile` unless you want it regenerated
- preferred locations for filtering, screening, scoring, and report display now come from `candidate_profile.json`

## Running Locally

Standard run:

```powershell
python main.py
```

This uses your local `data/candidate_profile.json` (ignored by git; never commit the real one). `--resume` is only required when creating or refreshing the profile. First-time setup: place your own `resume.pdf` next to `main.py`, then run `python main.py --resume resume.pdf --refresh-candidate-profile`. Profile refresh in GitHub Actions is not supported because the resume is local-only; automation reuses the cached profile.

Single company:

```powershell
python main.py --company "Razorpay"
```

Multiple companies:

```powershell
python main.py --company "Razorpay" --company "Visa"
```

Force re-score:

```powershell
python main.py --force-rescore
```

Refresh the candidate profile from the current resume:

```powershell
python main.py --resume resume.pdf --refresh-candidate-profile
```

Skip Telegram for a local run:

```powershell
python main.py --skip-telegram
```

Show cached debug payload for one job:

```powershell
python main.py --show-job-debug 4667645005
```

Local Telegram behavior:
- if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, local runs send the summary and HTML report to Telegram
- the Telegram success message for local runs is labeled as `Local run`

## Screening and Scoring Flow

The current runtime flow is:

1. deterministic title pre-filter
2. fast LLM screen
3. premium LLM score for jobs that survive stage 1

Stage 1:
- uses a compact candidate profile prompt
- uses a condensed job summary focused on qualifications and responsibilities
- returns one of:
  - `Pass`
  - `Borderline`
  - `Reject`
- can bypass premium scoring on a confident `Reject`

Stage 2:
- uses the richer candidate context
- uses the full prepared job description
- returns the final structured score

Current routing rule:
- confident stage-1 `Reject` can bypass premium scoring
- `Pass` and `Borderline` continue to premium scoring

## Report Behavior

The report includes:
- first-page jump links
- global filters
- role links to analysis cards in the first three sections
- external `Apply/Open` links
- `Why Waitlist` summaries for low-score jobs
- preferred-location note in the header
- a compact `Screen` indicator in the tables and detail cards

`Screen` currently shows:
- `Pass`
- `Borderline`
- `Reject`
- `N/A` for older rows without screen-stage data

Sections:
1. `New Findings`
2. `Cached Findings`
3. `Borderline`
4. `Waitlist`

## Telegram Delivery

Telegram delivery is available in two paths:
- local runs via `main.py`
- automation runs via `scripts/automation_runner.py`

Local runs:
- send the summary and generated HTML report when Telegram secrets are present
- can be disabled with `--skip-telegram`

Automation runs:
- use `scripts/automation_runner.py`
- send the summary and latest report artifact

## GitHub Actions Automation

The repo includes `.github/workflows/job-hunter.yml` for:
- scheduled daily runs
- manual `workflow_dispatch`
- optional `company` input
- optional `force_rescore`
- Telegram summary and report delivery
- persisted state across runs

Required repository secrets:

```text
OPENROUTER_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Recommended repository variables:

```text
OPENROUTER_MODEL
OPENROUTER_FAST_MODEL
OPENROUTER_PREMIUM_MODEL
OPENROUTER_QA_MODEL
```

Optional repository secrets for stage-specific API keys:

```text
OPENROUTER_FAST_API_KEY
OPENROUTER_PREMIUM_API_KEY
OPENROUTER_QA_API_KEY
TYPESAFE_API_KEY
```

Optional repository secrets for yogya site integration:

```text
SITE_INGEST_URL
SITE_INGEST_TOKEN
```

Optional repository variables:

```text
FAST_PROVIDER
TYPESAFE_MODEL
```

GitHub Actions now mirrors local environment handling:
- if stage-specific model variables are unset, the runtime falls back to `OPENROUTER_MODEL`
- if stage-specific API key secrets are unset, the runtime falls back to `OPENROUTER_API_KEY`
- if Telegram secrets are unset, the run still completes and skips Telegram delivery

Compatibility notes:
- `OPENROUTER_MODEL` and `config.model` still work as fallback for the premium model
- the code now supports explicit fast/premium/QA model selection

State persisted between runs:
- `data/job_history.db`
- `data/company_cache.json`
- stable output files used by automation

## Config Reference

`config.json` now focuses on run configuration only. Candidate intent, including preferred locations, lives in `candidate_profile.json`. Model selection can come from environment variables or a fallback `model` field in `config.json`.

Example:

```json
{
  "recency_days": 30,
  "companies": []
}
```

Notes:
- `OPENROUTER_PREMIUM_MODEL` is the preferred source of truth for the premium scorer
- `OPENROUTER_FAST_MODEL` is used by the fast screener
- `OPENROUTER_QA_MODEL` is reserved for the QA stage
- set `FAST_PROVIDER=typesafe` to run the fast screener on TypeSafe's Jev model via `TYPESAFE_API_KEY` (falls back to OpenRouter when unset)
- `config.json` may still contain `model` as a fallback
- target roles, avoid roles, seniority, matcher notes, and preferred locations now come from `candidate_profile.json`

## Debugging and Inspection

You can inspect one cached job in detail with:

```powershell
python main.py --show-job-debug <job_id>
```

This returns:
- job metadata
- prepared job description
- structured analysis
- stage-1 screen result
- stage-1 raw prompts
- stage-1 raw model output
- stage-1 raw API response JSON
- stage-2 raw prompts
- stage-2 raw model output
- stage-2 raw reasoning
- stage-2 raw API response JSON
- scoring version

Current practical use cases:
- understand why a role was screened out
- compare stage-1 and stage-2 behavior
- inspect malformed or empty provider responses
- validate prompt changes against recent jobs

## Project Structure

```text
jobHunter_AG_o4.6/
  main.py
  onboard.py
  query_db.py
  config.json
  requirements.txt
  scripts/
    automation_runner.py
  src/
    analyzer.py
    candidate_profile.py
    config_loader.py
    job_cache.py
    reporter.py
    resume_parser.py
    screener.py
    telegram_notifier.py
    title_filter.py
    scrapers/
      base.py
      greenhouse.py
      smart_recruiters.py
      workday.py
  templates/
    report_template.html
  data/
    candidate_profile.json
    job_history.db
    company_cache.json
  output/
    report_*.html
```

## Current Notes

- scoring is still being calibrated against user judgment
- candidate preferences are intentionally prompt-oriented for now
- cached and fresh jobs now preserve the same structured analysis quality
- parse-failure retry is enabled to recover malformed model JSON before accepting a failed score
- stage-1 screening is compact and heuristic-driven, but still intentionally conservative around uncertain roles
- QA stage is not yet active, even though config keys and cache/debug placeholders exist
- local Telegram delivery is available when Telegram secrets are configured

## Planned Next Phases

- optional LLM QA review for non-cached jobs only, to flag possible false positives and false negatives
- further score calibration against user-labeled jobs across role families
- generic role-family logic improvements that stay resume-agnostic and company-agnostic
- smarter distinction between mandatory requirements and strong preferences
- optional use of candidate preferences to improve borderline decisions
