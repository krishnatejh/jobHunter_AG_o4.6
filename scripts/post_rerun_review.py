"""Post-rerun analysis — review fresh scoring results."""
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
print(f"=== POST-RERUN ANALYSIS ({total} total jobs) ===\n")

# ---- Score distribution ----
print("--- SCORE DISTRIBUTION ---")
rows = conn.execute("SELECT score FROM job_history").fetchall()
buckets = {"Strong (70+)": 0, "Borderline (50-69)": 0, "Waitlist (1-49)": 0, "Zero/Reject (0)": 0}
for r in rows:
    s = r["score"]
    if s >= 70: buckets["Strong (70+)"] += 1
    elif s >= 50: buckets["Borderline (50-69)"] += 1
    elif s > 0: buckets["Waitlist (1-49)"] += 1
    else: buckets["Zero/Reject (0)"] += 1
for k, v in buckets.items():
    print(f"  {k}: {v}")

# ---- Screen decisions ----
print("\n--- SCREEN DECISIONS ---")
rows = conn.execute("SELECT screen_result, screen_model_used, score FROM job_history").fetchall()
dec_stats = {}
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "none")
    model = r["screen_model_used"] or "none"
    source = "deterministic" if model == "deterministic" else ("llm" if model != "none" else "none")
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

# ---- Strong matches (70+) ----
print("\n--- STRONG MATCHES (70+) ---")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, location_signal,
           screen_result, scoring_version, analyzed_at
    FROM job_history WHERE score >= 70 ORDER BY score DESC
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr = sr.get("decision", "n/a")
    print(f"  [{r['score']}] {r['title']} @ {r['company']} (v{r['scoring_version']})")
    print(f"       role={r['role_family_match']} sen={r['seniority_fit']} loc={r['location_signal']} screen={scr}")

# ---- Borderline (50-69) ----
print("\n--- BORDERLINE (50-69) ---")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, 
           screen_result, scoring_version
    FROM job_history WHERE score BETWEEN 50 AND 69 ORDER BY score DESC
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr = sr.get("decision", "n/a")
    print(f"  [{r['score']}] {r['title']} @ {r['company']} (v{r['scoring_version']}) role={r['role_family_match']} sen={r['seniority_fit']} scr={scr}")

# ---- Questionable screen rejects ----
print("\n--- SCREEN REJECTS (target-role titles) ---")
target_hints = ["architect", "director", "product manager", "engineering manager",
                "platform", "delivery", "program manager", "vp", "head of", "senior manager", "sr. manager"]
rows = conn.execute("""
    SELECT title, company, screen_result, screen_model_used, scoring_version
    FROM job_history WHERE score = 0 ORDER BY analyzed_at DESC
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "")
    title_lower = r["title"].lower()
    if any(hint in title_lower for hint in target_hints):
        reason = sr.get("reason", "")[:100]
        print(f"  {r['title']} @ {r['company']} | {dec} ({r['screen_model_used']})")
        print(f"    {reason}")

# ---- Waitlist with target-role titles ----
print("\n--- WAITLIST JOBS WITH TARGET-ROLE TITLES (potential false negatives) ---")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, gaps, screen_result
    FROM job_history WHERE score BETWEEN 1 AND 49 ORDER BY score DESC
""").fetchall()
for r in rows:
    title_lower = r["title"].lower()
    if any(hint in title_lower for hint in target_hints):
        sr = json.loads(r["screen_result"] or "{}")
        scr = sr.get("decision", "n/a")
        gaps = json.loads(r["gaps"] or "[]")
        gap1 = gaps[0][:80] if gaps else "none"
        print(f"  [{r['score']}] {r['title']} @ {r['company']} | role={r['role_family_match']} sen={r['seniority_fit']} scr={scr}")
        print(f"    gap: {gap1}")

# ---- Version distribution ----
print("\n--- SCORING VERSION DISTRIBUTION ---")
rows = conn.execute("""
    SELECT scoring_version, COUNT(*) as cnt, ROUND(AVG(score),1) as avg
    FROM job_history GROUP BY scoring_version ORDER BY scoring_version
""").fetchall()
for r in rows:
    print(f"  {r['scoring_version'] or 'unset'}: {r['cnt']} jobs, avg_score={r['avg']}")

conn.close()
