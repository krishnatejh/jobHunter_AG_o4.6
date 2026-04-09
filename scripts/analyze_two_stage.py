"""Analyze effectiveness of the two-stage LLM screening approach."""
import sqlite3
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

conn = sqlite3.connect("data/job_history.db")
conn.row_factory = sqlite3.Row

total = conn.execute("SELECT COUNT(*) as c FROM job_history").fetchone()["c"]
print(f"=== TWO-STAGE LLM EFFECTIVENESS ANALYSIS ({total} total jobs) ===\n")

# ---- 1. Stage coverage ----
print("--- STAGE COVERAGE ---")
rows = conn.execute("""
    SELECT 
        screen_model_used,
        screen_result,
        score,
        title,
        company
    FROM job_history
""").fetchall()

has_screen = 0
no_screen = 0
deterministic_screen = 0
llm_screen = 0
screen_bypassed_premium = 0  # score=0 and screen reject
screen_to_premium = 0  # had screen, then got premium scored

for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    model = r["screen_model_used"] or ""
    decision = sr.get("decision", "")
    
    if not decision:
        no_screen += 1
    else:
        has_screen += 1
        if model == "deterministic":
            deterministic_screen += 1
        else:
            llm_screen += 1
        
        if r["score"] == 0 and decision == "reject":
            screen_bypassed_premium += 1
        else:
            screen_to_premium += 1

print(f"  Jobs with screen stage:     {has_screen}")
print(f"    - Deterministic screen:   {deterministic_screen}")
print(f"    - LLM fast screen:        {llm_screen}")
print(f"  Jobs without screen (legacy): {no_screen}")
print(f"  Screen rejects that bypassed premium: {screen_bypassed_premium}")
print(f"  Screen -> premium scored:   {screen_to_premium}")

# ---- 2. Fast screen decision vs premium outcome ----
print("\n--- FAST SCREEN DECISION vs PREMIUM SCORE ---")
print("  (For jobs that went through BOTH stages)\n")

decision_vs_score = {}
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    decision = sr.get("decision", "")
    if not decision or r["score"] == 0:
        continue  # skip no-screen and bypassed jobs
    
    bucket = "strong (70+)" if r["score"] >= 70 else "borderline (50-69)" if r["score"] >= 50 else "waitlist (1-49)"
    key = (decision, bucket)
    if key not in decision_vs_score:
        decision_vs_score[key] = []
    decision_vs_score[key].append({
        "title": r["title"],
        "company": r["company"],
        "score": r["score"],
    })

for decision in ["pass", "review", "borderline", "reject"]:
    for bucket in ["strong (70+)", "borderline (50-69)", "waitlist (1-49)"]:
        key = (decision, bucket)
        items = decision_vs_score.get(key, [])
        if items:
            print(f"  Screen={decision:10} -> Premium={bucket:20}: {len(items)} jobs")
            for item in items[:3]:
                print(f"    [{item['score']}] {item['title']} @ {item['company']}")

# ---- 3. Screen bypass accuracy ----
print("\n--- SCREEN BYPASS ACCURACY ---")
print("  (Jobs rejected by screen that never got premium scoring)\n")

det_rejects = []
llm_rejects = []
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    decision = sr.get("decision", "")
    model = r["screen_model_used"] or ""
    
    if decision == "reject" and r["score"] == 0:
        entry = {
            "title": r["title"],
            "company": r["company"],
            "confidence": sr.get("confidence", 0),
            "reason": sr.get("reason", "")[:80],
            "role_fit": sr.get("role_family_fit", ""),
        }
        if model == "deterministic":
            det_rejects.append(entry)
        else:
            llm_rejects.append(entry)

print(f"  Deterministic rejects: {len(det_rejects)}")
# Categorize deterministic rejects by reason pattern
det_categories = {}
for r in det_rejects:
    reason = r["reason"].lower()
    if "experience band" in reason:
        cat = "Experience band too junior"
    elif "junior" in reason or "entry" in reason:
        cat = "Junior marker"
    elif "function" in reason or "role family" in reason:
        cat = "Function mismatch"
    elif "specialized" in reason or "domain" in reason:
        cat = "Specialized domain"
    elif "incomplete" in reason:
        cat = "Incomplete posting"
    else:
        cat = "Other"
    det_categories[cat] = det_categories.get(cat, 0) + 1

for cat, cnt in sorted(det_categories.items(), key=lambda x: -x[1]):
    print(f"    {cat}: {cnt}")

print(f"\n  LLM fast-screen rejects: {len(llm_rejects)}")
for r in llm_rejects[:10]:
    print(f"    {r['title']} @ {r['company']} (conf={r['confidence']}, role={r['role_fit']})")
    print(f"      {r['reason']}")

# ---- 4. Cost savings estimate ----
print("\n--- COST SAVINGS ESTIMATE ---")
premium_calls_avoided = screen_bypassed_premium
premium_calls_made = screen_to_premium + no_screen  # legacy + screen-passed
total_screen_calls = llm_screen
print(f"  Premium API calls avoided by screen bypass: {premium_calls_avoided}")
print(f"  Premium API calls made:                     {premium_calls_made}")
print(f"  Fast screen API calls made:                 {total_screen_calls}")
if premium_calls_avoided + premium_calls_made > 0:
    savings_pct = premium_calls_avoided / (premium_calls_avoided + premium_calls_made) * 100
    print(f"  Premium call reduction:                     {savings_pct:.1f}%")
    print(f"  Net API calls saved:                        {premium_calls_avoided - total_screen_calls}")

# ---- 5. Screen-pass accuracy (did pass jobs actually score well?) ----
print("\n--- SCREEN PASS ACCURACY ---")
pass_jobs = []
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    if sr.get("decision") == "pass" and r["score"] > 0:
        pass_jobs.append({"title": r["title"], "company": r["company"], "score": r["score"]})

if pass_jobs:
    avg = sum(j["score"] for j in pass_jobs) / len(pass_jobs)
    print(f"  Jobs that got screen=pass: {len(pass_jobs)}, avg premium score: {avg:.0f}")
    for j in sorted(pass_jobs, key=lambda x: -x["score"]):
        print(f"    [{j['score']}] {j['title']} @ {j['company']}")
else:
    print("  No pass-then-premium-scored jobs found.")

# ---- 6. Screen-review accuracy ----
print("\n--- SCREEN REVIEW ACCURACY ---")
review_jobs = []
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "")
    if dec in ("review", "borderline") and r["score"] > 0:
        review_jobs.append({"title": r["title"], "company": r["company"], "score": r["score"]})

if review_jobs:
    avg = sum(j["score"] for j in review_jobs) / len(review_jobs)
    high = sum(1 for j in review_jobs if j["score"] >= 70)
    mid = sum(1 for j in review_jobs if 50 <= j["score"] < 70)
    low = sum(1 for j in review_jobs if j["score"] < 50)
    print(f"  Jobs that got screen=review|borderline: {len(review_jobs)}, avg premium score: {avg:.0f}")
    print(f"    Strong (70+): {high}, Borderline (50-69): {mid}, Waitlist (<50): {low}")
    for j in sorted(review_jobs, key=lambda x: -x["score"])[:10]:
        print(f"    [{j['score']}] {j['title']} @ {j['company']}")
else:
    print("  No review-then-premium-scored jobs found.")

# ---- 7. Questionable screen rejects ----
print("\n--- POTENTIALLY QUESTIONABLE SCREEN REJECTS ---")
print("  (Deterministic rejects where the title matches a target role)\n")

target_hints = ["architect", "director", "product manager", "engineering manager",
                "platform", "delivery", "program manager", "vp", "head of"]
questionable = []
for r in det_rejects + llm_rejects:
    title_lower = r["title"].lower()
    if any(hint in title_lower for hint in target_hints):
        questionable.append(r)

if questionable:
    for q in questionable:
        print(f"  {q['title']} @ {q['company']} | conf={q['confidence']} role={q['role_fit']}")
        print(f"    {q['reason']}")
else:
    print("  None found.")

conn.close()
