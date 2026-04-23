import os, sys
import psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
tables = sys.argv[1:]
for t in tables:
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (t,)
    )
    rows = cur.fetchall()
    print(f"\n# {t} ({len(rows)} cols)")
    for r in rows:
        print(f"  {r[0]:38s} {r[1]}")
