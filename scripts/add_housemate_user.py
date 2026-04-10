"""Add a housemate User row so they can pair via email (GET /api/pair/request).

Usage (from repo root, chili-env, DATABASE_URL in .env):

  conda activate chili-env
  python scripts/add_housemate_user.py "Full Name" email@example.com

Idempotent: if the email already exists, prints the existing row and exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python scripts/add_housemate_user.py <display_name> <email>")
        sys.exit(1)
    name = sys.argv[1].strip()
    email = sys.argv[2].strip().lower()
    if not email or "@" not in email:
        print("Invalid email.")
        sys.exit(1)

    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        dup = db.query(User).filter(User.email == email).first()
        if dup:
            print(
                f"OK — user already exists: id={dup.id} name={dup.name!r} email={dup.email!r}"
            )
            return

        base_name = name
        if db.query(User).filter(User.name == name).first():
            suffix = email.split("@", 1)[0].replace(".", "_")[:32]
            name = f"{base_name} ({suffix})"
            if db.query(User).filter(User.name == name).first():
                name = f"{base_name} {email}"

        u = User(name=name, email=email)
        db.add(u)
        db.commit()
        db.refresh(u)
        print(f"Created user id={u.id} name={u.name!r} email={u.email!r}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
