"""Analyze current scoring data to identify screening/matching issues."""
import sqlite3
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

conn = sqlite3.connect("data/job_history.db")
conn.row_factory = sqlite3.Row

# Distribution overview
print("=== SCORE DISTRIBUTION ===")
rows = conn.execute("""
    SELECT score, COUNT(*) as cnt
    FROM job_history
    GROUP BY score
    ORDER BY score DESC
""").fetchall()
buckets = {"Strong (70+)": 0, "Borderline (50-69)": 0, "Waitlist (1-49)": 0, "Zero/Reject (0)": 0}
for r in rows:
    s = r["score"]
    if s >= 70:
        buckets["Strong (70+)"] += r["cnt"]
    elif s >= 50:
        buckets["Borderline (50-69)"] += r["cnt"]
    elif s > 0:
        buckets["Waitlist (1-49)"] += r["cnt"]
    else:
        buckets["Zero/Reject (0)"] += r["cnt"]
for k, v in buckets.items():
    print(f"  {k}: {v}")

# Screen decision distribution
print("\n=== SCREEN DECISIONS ===")
rows = conn.execute("""
    SELECT screen_result, score
    FROM job_history 
    WHERE screen_result != '{}' AND screen_result != ''
""").fetchall()
decision_stats = {}
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "unknown")
    if dec not in decision_stats:
        decision_stats[dec] = {"count": 0, "scores": []}
    decision_stats[dec]["count"] += 1
    decision_stats[dec]["scores"].append(r["score"])
for dec, stats in sorted(decision_stats.items(), key=lambda x: -x[1]["count"]):
    avg = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
    print(f"  {dec}: {stats['count']} jobs, avg_score={avg:.1f}")

# High scorers
print("\n=== HIGH SCORERS (70+) ===")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, location_signal,
           screen_result, screen_model_used
    FROM job_history WHERE score >= 70 ORDER BY score DESC LIMIT 15
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr_dec = sr.get("decision", "n/a")
    print(f"  [{r['score']}] {r['title']} @ {r['company']}")
    print(f"       role={r['role_family_match']} sen={r['seniority_fit']} loc={r['location_signal']} screen={scr_dec}")

# Borderline
print("\n=== BORDERLINE (50-69) ===")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit,
           screen_result
    FROM job_history WHERE score BETWEEN 50 AND 69 ORDER BY score DESC LIMIT 15
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr_dec = sr.get("decision", "n/a")
    print(f"  [{r['score']}] {r['title']} @ {r['company']} | role={r['role_family_match']} sen={r['seniority_fit']} scr={scr_dec}")

# Zero-score screen rejects that bypassed premium
print("\n=== ZERO-SCORE SCREEN REJECTS (bypassed premium) ===")
rows = conn.execute("""
    SELECT title, company, screen_result, screen_model_used
    FROM job_history 
    WHERE score = 0 AND screen_model_used != ''
    ORDER BY analyzed_at DESC LIMIT 20
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    dec = sr.get("decision", "?")
    conf = sr.get("confidence", 0)
    role_fit = sr.get("role_family_fit", "?")
    reason = sr.get("reason", "")[:120]
    model = r["screen_model_used"]
    print(f"  {r['title']} @ {r['company']} | {dec}({conf}) role={role_fit} model={model}")
    print(f"    reason: {reason}")

# Waitlist (1-49) — potential false negatives
print("\n=== WAITLIST (1-49) - potential false negatives ===")
rows = conn.execute("""
    SELECT title, company, score, role_family_match, seniority_fit, 
           screen_result, gaps
    FROM job_history WHERE score BETWEEN 1 AND 49 ORDER BY score DESC LIMIT 15
""").fetchall()
for r in rows:
    sr = json.loads(r["screen_result"] or "{}")
    scr_dec = sr.get("decision", "n/a")
    gaps = json.loads(r["gaps"] or "[]")
    gap_summary = " | ".join(gaps[:2]) if gaps else "no gaps listed"
    print(f"  [{r['score']}] {r['title']} @ {r['company']} | role={r['role_family_match']} sen={r['seniority_fit']} scr={scr_dec}")
    print(f"    gaps: {gap_summary[:140]}")

conn.close()
