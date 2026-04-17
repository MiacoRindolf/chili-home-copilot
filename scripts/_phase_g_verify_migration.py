"""One-shot verification of Phase G migration 133 inside chili."""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402


def main() -> int:
    with engine.connect() as c:
        rows = list(c.execute(text(
            "SELECT version_id FROM schema_version WHERE version_id LIKE '133%'"
        )))
        print("schema_version 133:", rows)
        tabs = list(c.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name LIKE 'trading_bracket%' "
            "ORDER BY table_name"
        )))
        print("tables:", tabs)
        for t in ("trading_bracket_intents", "trading_bracket_reconciliation_log"):
            cols = list(c.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name=:t ORDER BY ordinal_position"
            ), {"t": t}))
            print(f"{t} cols:")
            for n, ty in cols:
                print(" ", n, ty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
