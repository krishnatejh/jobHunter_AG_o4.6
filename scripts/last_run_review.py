"""Review ONLY the last run's results by finding the most recent analyzed_at cluster."""
import sqlite3
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

conn = sqlite3.connect("data/job_history.db")
conn.row_factory = sqlite3.Row

# Find the last run window — jobs analyzed in the most recent session
# First, show the analyzed_at distribution to find the run boundary
print("=== RECENT ANALYZED_AT TIMESTAMPS ===")
rows = conn.execute("""
    SELECT analyzed_at, COUNT(*) as cnt
    FROM job_history
    GROUP BY substr(analyzed_at, 1, 13)
    ORDER BY analyzed_at DESC
    LIMIT 20
""").fetchall()
for r in rows:
    print(f"  {r['analyzed_at']}: {r['cnt']} jobs")

# Get the latest analyzed_at date
latest = conn.execute("SELECT MAX(analyzed_at) as m FROM job_history").fetchone()["m"]
print(f"\nLatest analyzed_at: {latest}")

# Find all jobs analyzed on the same day as the latest
latest_date = latest[:10]  # YYYY-MM-DD
print(f"Filtering to date: {latest_date}")

total = conn.execute(
    "SELECT COUNT(*) as c FROM job_history WHERE analyzed_at >= ?",
    (latest_date,)
).fetchone()["c"]
print(f"Jobs from last run date: {total}\n")

# Now do full analysis on just these jobs
print(f"=== LAST RUN ANALYSIS ({total} jobs, {latest_date}) ===\n")

# Score distribution
print("--- SCORE DISTRIBUTION ---")
rows = conn.execute(
    "SELECT score FROM job_history WHERE analyzed_at >= ?", (latest_date,)
).fetchall()
buckets = {"Strong (70+)": 0, "Borderline (50-69)": 0, "Waitlist (1-49)": 0, "Zero/Reject (0)": 0}
for r in rows:
    s = r["score"]
    if s >= 70: buckets["Strong (70+)"] += 1
    elif s >= 50: buckets["Borderline (50-69)"] += 1
    elif s > 0: buckets["Waitlist (1-49)"] += 1
    else: buckets["Zero/Reject (0)"] += 1
for k, v in buckets.items():
    print(f"  {k}: {v}")

# Screen decisions
print("\n--- SCREEN DECISIONS ---")
rows = conn.execute(
    "SELECT screen_result, screen_model_used, score FROM job_history WHERE analyzed_at >= ?",
    (latest_date,)
).fetchall()
dec_stats = {}
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "none")
    model = r["screen_model_used"] or "none"
    source = "deterministic" if model == "deterministic" else ("llm" if model not in ("none", "") else "no-screen")
    key = f"{dec} ({source})"
    if key not in dec_stats:
        dec_stats[key] = {"count": 0, "scores": [], "bypassed": 0}
    dec_stats[key]["count"] += 1
    dec_stats[key]["scores"].append(r["score"])
    if dec == "reject" and r["score"] == 0:
        dec_stats[key]["bypassed"] += 1

for key in sorted(dec_stats.keys()):
    s = dec_stats[key]
    avg = sum(s["scores"]) / len(s["scores"]) if s["scores"] else 0
    bypass_note = f" (bypassed premium: {s['bypassed']})" if s["bypassed"] else ""
    print(f"  {key}: {s['count']} jobs, avg_score={avg:.1f}{bypass_note}")

# Strong matches
print("\n--- STRONG MATCHES (70+) ---")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, location_signal,
           screen_result, scoring_version
    FROM job_history WHERE analyzed_at >= ? AND score >= 70 ORDER BY score DESC
""", (latest_date,)).fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr = sr.get("decision", "n/a")
    print(f"  [{r['score']}] {r['title']} @ {r['company']}")
    print(f"       role={r['role_family_match']} sen={r['seniority_fit']} loc={r['location_signal']} screen={scr}")

# Borderline
print("\n--- BORDERLINE (50-69) ---")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, screen_result
    FROM job_history WHERE analyzed_at >= ? AND score BETWEEN 50 AND 69 ORDER BY score DESC
""", (latest_date,)).fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr = sr.get("decision", "n/a")
    print(f"  [{r['score']}] {r['title']} @ {r['company']} | role={r['role_family_match']} sen={r['seniority_fit']} scr={scr}")

# Screen rejects with target-role titles
print("\n--- SCREEN REJECTS (target-role titles only) ---")
target_hints = ["architect", "director", "product manager", "engineering manager",
                "platform", "delivery", "program manager", "vp", "head of", "senior manager", "sr. manager"]
rows = conn.execute("""
    SELECT title, company, screen_result, screen_model_used
    FROM job_history WHERE analyzed_at >= ? AND score = 0 ORDER BY title
""", (latest_date,)).fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "")
    title_lower = r["title"].lower()
    if any(hint in title_lower for hint in target_hints):
        reason = sr.get("reason", "")[:120]
        print(f"  {r['title']} @ {r['company']} | {dec} ({r['screen_model_used']})")
        print(f"    {reason}")

# Waitlist with target-role titles
print("\n--- WAITLIST TARGET-ROLE TITLES (potential false negatives) ---")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, gaps, screen_result
    FROM job_history WHERE analyzed_at >= ? AND score BETWEEN 1 AND 49 ORDER BY score DESC
""", (latest_date,)).fetchall()
for r in rows:
    title_lower = r["title"].lower()
    if any(hint in title_lower for hint in target_hints):
        sr = json.loads(r["screen_result"] or "{}")
        scr = sr.get("decision", "n/a")
        gaps = json.loads(r["gaps"] or "[]")
        gap1 = gaps[0][:100] if gaps else "none"
        print(f"  [{r['score']}] {r['title']} @ {r['company']} | role={r['role_family_match']} sen={r['seniority_fit']} scr={scr}")
        print(f"    gap: {gap1}")

# All zero-score rejects
print("\n--- ALL SCREEN REJECTS (zero-score) ---")
rows = conn.execute("""
    SELECT title, company, screen_result, screen_model_used
    FROM job_history WHERE analyzed_at >= ? AND score = 0 ORDER BY company, title
""", (latest_date,)).fetchall()
print(f"  Total: {len(rows)}")
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "?")
    model = r["screen_model_used"] or "?"
    source = "det" if model == "deterministic" else "llm"
    reason = sr.get("reason", "")[:80]
    print(f"  [{source}] {r['title']} @ {r['company']}")
    print(f"    {reason}")

conn.close()
