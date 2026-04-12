# Functional Review — jobHunter_AG_o4.6

**Focus**: Does this tool reliably automate "go to career pages → find suitable roles → score via LLM"?

---

## Verdict: It Works — But Several Gaps Undermine Reliability

The pipeline successfully scrapes 3 ATS platforms, sends job descriptions + your resume to an LLM, and produces a scored HTML report. This is a solid personal-use v1. However, the **scoring reliability** — the core value proposition — has specific weaknesses that cause false positives, false negatives, and wasted LLM calls.

Below are **functional** issues (not code hygiene), grouped by how much they affect the goal of reliably matching you to the right roles.

---

## 🔴 Critical — Directly Hurts Match Reliability

### 1. No Pre-Filter = Every Scraped Job Hits the LLM ($$)

**Problem**: Every job that passes recency + location filters gets sent for LLM scoring — including obviously irrelevant roles (Intern, VP, HR Coordinator, Sales Rep). Your `SEVERE_BLOCKER_PATTERNS` and `candidate_preferences.avoid_role_families` exist but are only applied *after* the LLM call.

**Impact**: 
- Wastes ~60-70% of your LLM budget on jobs any keyword check would reject
- With 4 companies × 50+ open roles = 200+ LLM calls per run at ~$0.01-0.05 each

**Suggestion**: Add a **lightweight title-based pre-filter** before the LLM call:
```python
# In main.py, before score_job():
SKIP_TITLE_PATTERNS = [
    r"\bintern\b", r"\bassociate\b", r"\bvp\b", r"\bdirector\b",
    r"\bsales\s+rep", r"\bcustomer\s+service\b",
]
REQUIRE_TITLE_KEYWORDS = ["architect", "manager", "product", "delivery", "solution"]

# Skip if title matches exclusions OR doesn't match any inclusion
```

This is a 15-minute change that could cut LLM calls by 50-70%.

---

### 2. LLM Score Is Too Compressed — Hard to Distinguish Good vs Great

**Problem**: Your 6-component weighted scoring produces a narrow range. Looking at the math:
- Weights sum to 1.0 ✓ but 5 of 6 components cluster 60-80 for "adjacent" roles
- After weights, most scores land in **55-75** range before penalties
- The penalty system then applies up to **–58 points** of deductions (18 missing reqs + 16 blocker + 12 role mismatch + 12 location)
- Result: Real candidates get 60-72, poor fits get 40-55, genuinely great fits get 72-80

**Impact**: The "Good" (70-84) and "Borderline" (40-69) buckets are overcrowded. Many legitimately interesting roles score 65-69 and end up in "Borderline" which you might ignore.

**Suggestions**:
1. Widen the weight for `role_alignment` (from 0.25→0.30) — this is your strongest signal
2. Reduce the `missing_items` penalty cap from 18 to 12 — the LLM already penalizes via low `technical_skills`
3. Add a **"golden match" bonus** (+5-8 points) when ALL of: role=strong, seniority=matched, location=matched

---

### 3. [candidate_preferences](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#283-307) Are "Soft Guidance" — Should Be Harder

**Problem**: The prompt says *"Treat Candidate Preferences as directional guidance, not absolute blockers"* and *"Do not override explicit hard requirements"*. This is too permissive. Your `avoid_role_families: ["Sales", "Alliances", "Account Management", "HR"]` should be treated as *hard exclusions* — you will never apply for these.

**Impact**: The LLM scores "Senior Sales Manager" at 35-45 instead of 0, and it still shows up in your report as "Borderline" instead of being filtered out entirely.

**Suggestion**: Split preferences into two tiers:
```json
{
  "exclude_role_families": ["Sales", "HR", "Account Management"],
  "prefer_role_families": ["Solution Architect", "Product Manager"],
  "soft_preferences": { "seniority": "manager_to_senior_manager" }
}
```
Hard exclusions get a zero-cost title filter, soft preferences stay as LLM guidance.

---

### 4. SmartRecruiters Uses API Slug as Company Name

**Problem**: [smart_recruiters.py:196](file:///d:/Agents/jobHunter_AG_o4.6/src/scrapers/smart_recruiters.py#L196) sets `"company": slug` (e.g., `"Visa"`) instead of `company_config.get("company_name", slug)`. 

**Impact**: Cache key mismatch — `JobCache.make_job_key()` uses [company](file:///d:/Agents/jobHunter_AG_o4.6/onboard.py#56-64) in the fallback hash path. If the config says `"Visa Inc"` but the scraper emits `"Visa"`, the same job will be scored twice after any company name change.

**Fix**: One-line change:
```diff
-            "company": slug
+            "company": company_config.get("company_name", slug)
```

---

## 🟠 High — Causes Silent Failures or Missed Jobs

### 5. Greenhouse Uses `updated_at` Not `created_at` for Recency

**Problem**: [greenhouse.py:269](file:///d:/Agents/jobHunter_AG_o4.6/src/scrapers/greenhouse.py#L269) uses `updated_at` for the recency filter. But Greenhouse jobs get `updated_at` bumped every time the description is edited. A job posted 90 days ago but edited yesterday passes the 30-day filter.

**Impact**: Old, stale jobs appear as "new" in your report.

**Fix**: Use `first_published_at` from the Greenhouse API response if available, and fall back to `updated_at`.

---

### 6. Location Filter Is Too Strict — Misses Multi-Location Jobs

**Problem**: Location matching is `pref.lower() in loc_lower` (substring match). For multi-location postings like `"New York, NY; Bangalore, India; London, UK"`, this works. But for postings like `"Multiple Locations"` or `"India - Multiple Cities"`, the check fails if your preference is `"Bangalore"`.

**Impact**: Legitimate jobs in preferred locations get silently filtered out.

**Suggestion**: Add `"multiple"`, `"various"`, `"any"` as auto-pass keywords in location filters:
```python
LOCATION_PASS_KEYWORDS = {"multiple", "various", "any", "remote", "flexible"}

if any(keyword in loc_lower for keyword in LOCATION_PASS_KEYWORDS):
    pass  # Don't skip — let LLM evaluate location
```

---

### 7. Job Description Truncation Loses Key Requirements

**Problem**: [analyzer.py:267-272](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#L267-L272) truncates at 7000 chars: keeps first 4200 + last 2200. Many job descriptions put **qualifications and requirements in the middle** (after "About the Company" and before "Benefits/EEO"), which is exactly the section being dropped.

**Impact**: The LLM can't see the actual requirements list → scores based on job title + role description only → inflated scores for vaguely-aligned roles.

**Suggestion**: Smarter truncation that preserves requirements:
```python
def _prepare_description(description: str) -> str:
    if len(description) <= 7000:
        return description
    
    # Try to find and preserve requirements section
    req_patterns = ["requirements", "qualifications", "what you'll need", 
                    "what we're looking for", "must have", "skills"]
    
    for pattern in req_patterns:
        idx = description.lower().find(pattern)
        if idx > 0:
            # Keep intro (2000) + requirements section (3500) + tail (1500)
            intro = description[:2000]
            req_section = description[max(0, idx-200):idx+3300]
            tail = description[-1500:]
            return intro + "\n\n[... truncated ...]\n\n" + req_section + "\n\n[... truncated ...]\n\n" + tail
    
    # Fallback to original behavior
    return description[:4200] + "\n\n[... truncated ...]\n\n" + description[-2200:]
```

---

### 8. No Deduplication Across ATS Platforms

**Problem**: If a company posts the same role on multiple platforms (e.g., on their Workday instance AND on a Greenhouse board), the same job gets scored twice.

**Impact**: Wasted LLM calls and duplicate entries in the HTML report.

**Suggestion**: Before scoring, deduplicate by [(company, normalized_title, location)](file:///d:/Agents/jobHunter_AG_o4.6/scripts/automation_runner.py#120-146) — lightweight enough for personal use.

---

## 🟡 Medium — Annoyances That Erode Trust

### 9. Cache Does Not Re-Score When [candidate_preferences](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#283-307) Change

**Problem**: The cache invalidation checks `scoring_version` and `posted_date`, but not whether [candidate_preferences](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#283-307) changed. If you edit your target roles or add a new avoid family, cached scores are stale.

**Suggestion**: Hash [candidate_preferences](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#283-307) and include it in the cache lookup key, or bump `SCORING_VERSION` manually when preferences change (fragile but simple).

---

### 10. [prepare_results()](file:///d:/Agents/jobHunter_AG_o4.6/src/reporter.py#179-204) Called Twice

**Problem**: [main.py:290](file:///d:/Agents/jobHunter_AG_o4.6/main.py#L290) calls [prepare_results()](file:///d:/Agents/jobHunter_AG_o4.6/src/reporter.py#179-204), and [reporter.py:282](file:///d:/Agents/jobHunter_AG_o4.6/src/reporter.py#L282) calls it again inside [generate_report()](file:///d:/Agents/jobHunter_AG_o4.6/src/reporter.py#262-344). The function mutates in-place (sorts, adds fields), so the double-call is benign but adds confusion.

**Fix**: Remove the call inside [generate_report()](file:///d:/Agents/jobHunter_AG_o4.6/src/reporter.py#262-344).

---

### 11. Report Output Grows Unbounded

**Problem**: Every run creates a new `report_YYYYMMDD_HHMMSS.html` in the `output/` directory. No cleanup, no rotation.

**Suggestion**: Keep only the last N reports (e.g., 10) and auto-delete older ones.

---

### 12. No Summary of "What Changed Since Last Run"

**Problem**: The report shows all results, but there's no diff view. If you run daily, you care about **what's new** — not re-reading 150 cached entries.

**Suggestion**: The `cache_status` field exists (`new`/`cached`/`reposted`) but the HTML report could surface new items more prominently (e.g., a "🆕 New Since Last Run" section at the very top).

---

## 📊 Improvement Roadmap (by Impact)

| # | Issue | Impact on Goal | Effort | Quick Win? |
|---|-------|---------------|--------|-----------|
| 1 | Title pre-filter before LLM | Saves 50-70% of LLM cost | 30 min | ✅ |
| 3 | Hard exclusions for avoid_role_families | Eliminates noise jobs from report | 20 min | ✅ |
| 4 | SmartRecruiters company name fix | Prevents cache key drift | 5 min | ✅ |
| 7 | Smarter description truncation | Better scoring accuracy | 45 min | ✅ |
| 6 | Location filter auto-pass keywords | Stops missing multi-location jobs | 15 min | ✅ |
| 2 | Scoring range compression fix | Better differentiation of matches | 1 hr | |
| 5 | Greenhouse date field fix | Correct recency filtering | 15 min | ✅ |
| 8 | Cross-ATS deduplication | Saves LLM calls on multi-ATS companies | 45 min | |
| 9 | Cache → preferences hash | Ensures fresh scores after config changes | 30 min | |
| 10 | Remove duplicate prepare_results | Code clarity | 5 min | ✅ |
| 11 | Report rotation | Keeps output/ clean | 15 min | ✅ |
| 12 | "New since last run" in report | Better daily usage experience | 1 hr | |

---

## What's Working Well

- **Cache design is solid** — `SCORING_VERSION`-based invalidation and repost detection are exactly right for iterative personal use
- **Prompt engineering is above average** — the system prompt with component scores, hard/nice-to-have split, and role family classification is well-structured for structured output
- **Multi-ATS support** — the scraper abstraction works cleanly for all 3 platforms
- **Deterministic post-processing** — decoupling the LLM's component scores from the final score via [_compute_final_score()](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#376-412) adds repeatability
- **Your [_normalize_location()](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#309-319) and [_apply_location_preferences()](file:///d:/Agents/jobHunter_AG_o4.6/src/analyzer.py#334-374)** catch the Bengaluru/Bangalore variant gracefully
