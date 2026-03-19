"""Helper script to query the job history SQLite database."""

import argparse
import sqlite3
import json
from pathlib import Path
from tabulate import tabulate

DB_PATH = Path(__file__).resolve().parent / "data" / "job_history.db"

def main():
    parser = argparse.ArgumentParser(description="Query the job hunter SQLite database.")
    parser.add_argument("--recent", type=int, default=10, help="Show the N most recently analyzed jobs (default: 10)")
    parser.add_argument("--company", type=str, help="Filter by company name")
    parser.add_argument("--min-score", type=int, help="Filter by minimum score")
    parser.add_argument("--job-id", type=str, help="Look up a specific job by REQ ID")
    parser.add_argument("--stats", action="store_true", help="Show database statistics")
    
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Run main.py first to create the cache.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.stats:
        row = conn.execute("SELECT COUNT(*) as total, AVG(score) as avg_score, MAX(analyzed_at) as last_run FROM job_history").fetchone()
        print("\n=== Database Statistics ===")
        print(f"Total Cached Jobs: {row['total']}")
        print(f"Average Score:     {round(row['avg_score'] or 0)}")
        print(f"Last Run:          {row['last_run'] or 'Never'}")
        print("===========================\n")
        return

    query = "SELECT job_req_id, company, title, location, score, posted_date, analyzed_at FROM job_history WHERE 1=1"
    params = []

    if args.company:
        query += " AND company LIKE ?"
        params.append(f"%{args.company}%")
    
    if args.min_score:
        query += " AND score >= ?"
        params.append(args.min_score)

    if args.job_id:
        query += " AND job_req_id = ?"
        params.append(args.job_id)

    query += " ORDER BY analyzed_at DESC LIMIT ?"
    params.append(args.recent)

    rows = conn.execute(query, params).fetchall()

    if not rows:
        print("No jobs found matching the criteria.")
        return

    # Convert to list of dicts for tabulate
    table_data = []
    for r in rows:
        table_data.append([
            r["job_req_id"],
            r["company"],
            r["title"][:40] + ("..." if len(r["title"]) > 40 else ""),
            r["location"][:20],
            r["score"],
            r["posted_date"],
            r["analyzed_at"][:10]
        ])

    headers = ["Req ID", "Company", "Title", "Location", "Score", "Posted", "Analyzed"]
    print(f"\nShowing top {len(rows)} results:\n")
    print(tabulate(table_data, headers=headers, tablefmt="simple"))
    print()

if __name__ == "__main__":
    main()
