import os
import psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='public' ORDER BY table_name"
)
names = [r[0] for r in cur.fetchall()]
print("Total tables:", len(names))
for n in names:
    print(" ", n)
