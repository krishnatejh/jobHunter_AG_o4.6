# Local Backlog

This file is the working backlog for the local project state. It is intended for local use as the master task list across threads and can be updated as we learn more from runs, reviews, and tuning work.

Last updated: 2026-03-31

## Current Priorities

1. Tighten stage-1 `pass/review/reject` calibration.
2. Add incomplete-posting guardrails.
3. Add per-attempt screen audit trail.
4. Wire the QA reviewer stage.
5. Add single-job rerun support.

## Fixes

### 1. Stage-1 `pass` criteria is too loose
- Status: Pending
- Why it matters:
  - Latest run had `84 reject`, `38 review`, `1 pass`.
  - The one `pass` still became a weak premium score, which means stage 1 is not yet trustworthy for confident positive routing.
- Desired outcome:
  - `pass` should require clear role-family alignment, acceptable seniority, location alignment, and enough direct evidence.

### 2. Stage-1 falls back to `Review` too often
- Status: Pending
- Why it matters:
  - Good roles like `Technical Program Manager` and `Director, Product Development` survived mostly because of fallback wording like “insufficient information,” not because stage 1 made a strong judgment.
- Desired outcome:
  - Fewer soft fallbacks.
  - More intentional `review` decisions with concrete reasons.

### 3. Screen audit is not attempt-by-attempt
- Status: Completed
- Why it matters:
  - We currently store the final screen prompt/output, but not each failed parse/repair attempt separately.
  - This makes intermittent fast-model issues harder to debug.
- Desired outcome:
  - Persist attempt 1/2/3 prompt, raw output, parse error, and whether it was a repair attempt.
 - Implemented:
  - `screen_attempts` are now persisted in the cache/debug payload with per-attempt prompt, raw output, parse error, request error, and repair flag.

### 4. Incomplete jobs can still surface too high
- Status: Pending
- Why it matters:
  - Example observed: `Unknown @ Wells Fargo` with weak metadata still surfaced too high.
- Desired outcome:
  - Missing title or missing description should force reject or hard waitlist behavior.

### 5. Stage-1 terminology cleanup in docs is incomplete
- Status: Pending
- Why it matters:
  - Runtime now uses `Pass / Review / Reject`, but some docs still mention stage-1 `Borderline`.
- Desired outcome:
  - Documentation should match the live runtime language.

### 6. Fast-screen model/provider reliability still has rough edges
- Status: Monitoring
- Why it matters:
  - We still see occasional repair retries and empty/partial first responses.
- Desired outcome:
  - Better diagnostics and, if needed, improved model/provider selection.

### 7. `Filtered Out` report UX can be improved
- Status: Pending
- Why it matters:
  - Strong/borderline/waitlist are easy to inspect, but screened-out jobs are less visible.
- Desired outcome:
  - Easier review of what stage 1 blocked, without cluttering the main report.

## New Developments

### 8. QA reviewer stage is prepared but not active
- Status: Pending
- Current state:
  - Config, cache fields, audit placeholders, and report-readiness groundwork exist.
- Desired outcome:
  - Add the actual third-stage QA reviewer for suspicious high scores, borderline roles, and disagreement cases.

### 9. Per-attempt audit trail for screen and QA
- Status: In progress
- Why it matters:
  - We want true forensics, not just final-stage snapshots.
- Desired outcome:
  - Attempt-level prompt/output/error storage for both fast screen and future QA.
 - Current state:
  - Fast screen attempt-level audit is implemented.
  - QA attempt-level audit is still pending because QA is not active yet.

### 10. Stronger deterministic stage-1 gates
- Status: In progress
- Current state:
  - Implemented:
    - explicit junior experience bands like `2-8 years`
    - incomplete-posting rejection for truly unusable postings
    - strong function-family mismatch rejection beyond title
    - specialized ops/domain mismatch rejection
    - explicit junior marker rejection
  - Reverted:
    - mandatory certification/license mismatch rejection was rolled back after causing broad false positives in the April 2, 2026 force-rescore run
- Still to add:
  - tune thresholds and wording based on live runs
  - expand deterministic rules only if additional high-confidence patterns prove safe

### 11. Better `pass/review/reject` calibration logic
- Status: Pending
- Why it matters:
  - This is the biggest quality lever in the current system.
- Desired outcome:
  - A role should only get `pass` when it clears stronger role/seniority/evidence checks.

### 12. QA report indicators
- Status: Pending
- Desired outcome:
  - Once QA is live, add a light `QA` indicator similar to the existing `Screen` indicator.

### 13. Single-job rerun mode
- Status: Pending
- Desired outcome:
  - Add a clean `--job-id` or equivalent debug flow for one role end-to-end.

### 14. Cross-ATS dedupe
- Status: Deferred / Monitor
- Note:
  - Occasional duplicates are acceptable for now, but the item remains open.

### 15. Greenhouse recency accuracy
- Status: Pending
- Desired outcome:
  - Improve Greenhouse recency handling so reposts/edits do not look incorrectly fresh.

## Notes

- This file is intentionally local working memory for the project.
- Update this file when:
  - a pending item is completed
  - a new recurring issue is observed
  - priorities change after a run review
- Keep public docs like `README.md` focused on runtime behavior and user guidance; keep backlog planning here unless explicitly asked to promote an item into public documentation.
