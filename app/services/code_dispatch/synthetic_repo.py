# Strategy (b): no existing code_repos row used /app in this environment; the
# scheduler image mounts the app at /app. This helper inserts or reuses a
# global (user_id NULL) row so get_bound_workspace_repo_for_profile can resolve
# a reachable repo for operator-queued plan_tasks.
from __future__ import annotations

from sqlalchemy import or_
from datetime import datetime

from sqlalchemy.orm import Session

from ...models.code_brain import CodeRepo

_SYN_NAME = "chili-home-copilot"
_SYN_PATH = "/app"
_SYN_CONTAINER = "/app"


def ensure_synthetic_dispatch_repo(db: Session, user_id: int) -> CodeRepo:  # noqa: ARG001
    """Return an active CodeRepo for the in-container app tree (see strategy note above)."""
    r = (
        db.query(CodeRepo)
        .filter(
            CodeRepo.active.is_(True),
            or_(
                CodeRepo.name == _SYN_NAME,
                CodeRepo.path == _SYN_PATH,
                CodeRepo.container_path == _SYN_CONTAINER,
            ),
        )
        .order_by(CodeRepo.id.asc())
        .first()
    )
    if r is not None:
        return r
    row = CodeRepo(
        path=_SYN_PATH,
        name=_SYN_NAME,
        host_path=None,
        container_path=_SYN_CONTAINER,
        user_id=None,
        reachable_in_scheduler=True,
        reachable_in_web=True,
        active=True,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
