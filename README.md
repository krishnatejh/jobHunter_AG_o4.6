# Job Hunter Agent

An automated job discovery and scoring pipeline that scrapes supported ATS boards, filters jobs by recency and location, scores them against a resume with an LLM, caches results, and produces an interactive HTML report.

## What It Does

- supports multiple ATS sources:
  - Workday
  - SmartRecruiters
  - Greenhouse
- filters jobs before scoring using:
  - `recency_days`
  - `location_preferences`
- scores matched jobs with an LLM using:
  - resume text
  - configured preferred locations
  - optional `candidate_preferences` from `config.json`
- caches scored jobs in SQLite to avoid repeated LLM calls
- generates a report with four sections:
  - `New Findings`
  - `Cached Findings`
  - `Borderline`
  - `Waitlist`
- supports scheduled/manual GitHub Actions runs and Telegram delivery

## Current Scoring Model

The scorer is resume-based, prompt-auditable, and cache-versioned.

It now includes:
- structured JSON scoring from the LLM
- deterministic final score calculation in code
- parse-failure retry with JSON repair
- preferred-location handling from `config.json`
- audit storage for:
  - raw prompts
  - raw model output
  - structured analysis

The current report buckets are:
- `New Findings`: new/reposted jobs with score `>= 70`
- `Cached Findings`: cached jobs with score `>= 70`
- `Borderline`: jobs with score `40-69`
- `Waitlist`: jobs with score `< 40`

## Setup

```powershell
python -m venv venv
.\venv\Scripts\activate
python -m pip install -r requirements.txt
```

Create `.env` with:

```text
OPENROUTER_API_KEY=your_openrouter_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

## Onboarding Companies

Use `onboard.py` to discover and save company ATS endpoints.

```powershell
python onboard.py --company "Mastercard"
python onboard.py --company "Razorpay" --ats greenhouse --refresh
python onboard.py --list
```

## Running Locally

Standard run:

```powershell
python main.py --resume resume.pdf
```

Single company:

```powershell
python main.py --resume resume.pdf --company "Razorpay"
```

Multiple companies:

```powershell
python main.py --resume resume.pdf --company "Razorpay" --company "Visa"
```

Force re-score:

```powershell
python main.py --resume resume.pdf --force-rescore
```

Show cached debug payload for one job:

```powershell
python main.py --show-job-debug 4667645005
```

## GitHub Actions Automation

The repo includes `.github/workflows/job-hunter.yml` for:

- daily schedule at 7:30 PM IST (2:00 PM UTC)

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

State persisted between runs:
- `data/job_history.db`
- `data/company_cache.json`
- stable output files used by automation

## Config Reference

`config.json` now supports both run settings and candidate intent.

Example:

```json
{
  "model": "nvidia/nemotron-3-super-120b-a12b:free",
  "location_preferences": [
    "Remote",
    "India",
    "Bangalore",
    "Pune",
    "Bengaluru",
    "Mysore"
  ],
  "recency_days": 30,
  "candidate_preferences": {
    "target_roles": [
      "Solution Architect",
      "Delivery",
      "Product Manager",
      "Banking Solutions"
    ],
    "avoid_role_families": [
      "Sales",
      "Alliances",
      "Account Management",
      "HR"
    ],
    "seniority_preference": "manager_to_senior_manager",
    "primary_domains": [
      "Banking",
      "Payments",
      "Fintech",
      "Core Banking"
    ],
    "notes_for_matcher": "Adjacent product and banking strategy roles are acceptable; customer-facing TAM and pure sales are lower priority."
  },
  "companies": []
}
```

Notes:
- `location_preferences` are used both for pre-filtering and for location scoring guidance
- `candidate_preferences` are currently used as prompt guidance only
- `candidate_preferences` do not directly alter deterministic score math

## Report Behavior

The report includes:
- first-page jump links
- global filters
- role links to analysis cards in the first three sections
- external `Apply/Open` links
- `Why Waitlist` summaries for low-score jobs
- preferred-location note in the header

Sections:
1. `New Findings`
2. `Cached Findings`
3. `Borderline`
4. `Waitlist`

## Debugging and Inspection

You can inspect one cached job in detail with:

```powershell
python main.py --show-job-debug <job_id>
```

This returns:
- job metadata
- prepared job description
- structured analysis
- raw prompts
- raw model output
- scoring version

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
    config_loader.py
    job_cache.py
    reporter.py
    resume_parser.py
    telegram_notifier.py
    scrapers/
      base.py
      greenhouse.py
      smart_recruiters.py
      workday.py
  templates/
    report_template.html
  data/
    job_history.db
    company_cache.json
  output/
    report_*.html
```

## Current Notes

- scoring is still being calibrated against user judgment
- candidate preferences are intentionally prompt-only for now
- cached and fresh jobs now preserve the same structured analysis quality
- parse-failure retry is enabled to recover malformed model JSON before accepting a failed score

## Planned Next Phases

- optional LLM QA review for non-cached jobs only, to flag possible false positives and false negatives
- further score calibration against user-labeled jobs across role families
- generic role-family logic improvements that stay resume-agnostic and company-agnostic
- smarter distinction between mandatory requirements and strong preferences
- optional use of candidate_preferences from config.json as prompt guidance to improve borderline decisions


