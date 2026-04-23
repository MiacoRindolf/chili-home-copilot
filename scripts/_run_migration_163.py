"""Apply migration 163 (CPCV promotion gate columns on scan_patterns)."""
from dotenv import load_dotenv
load_dotenv()
from app.db import engine
from app.migrations import _migration_163_cpcv_promotion_gate_evidence

with engine.connect() as conn:
    _migration_163_cpcv_promotion_gate_evidence(conn)
    print("migration 163 applied")
