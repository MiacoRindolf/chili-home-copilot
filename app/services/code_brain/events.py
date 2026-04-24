"""Ownership and visibility helpers for Code Brain learning events."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ...models import User
from ...models.code_brain import CodeLearningEvent, CodeRepo


def _all_user_ids(db: Session) -> list[int]:
    return [int(user_id) for (user_id,) in db.query(User.id).order_by(User.id.asc()).all() if user_id is not None]


def resolve_learning_event_user_ids(
    db: Session,
    *,
    explicit_user_id: int | None = None,
    repo: CodeRepo | None = None,
    repos: Sequence[CodeRepo] | None = None,
) -> list[int | None]:
    if explicit_user_id is not None:
        return [int(explicit_user_id)]

    target_user_ids: set[int] = set()
    shared_repo_visible = False
    repo_rows: list[CodeRepo] = []
    if repo is not None:
        repo_rows.append(repo)
    if repos:
        repo_rows.extend(row for row in repos if row is not None)

    for row in repo_rows:
        if row.user_id is None:
            shared_repo_visible = True
            continue
        target_user_ids.add(int(row.user_id))

    if shared_repo_visible:
        target_user_ids.update(_all_user_ids(db))

    if target_user_ids:
        return sorted(target_user_ids)
    return [None]


def log_learning_event(
    db: Session,
    *,
    repo_id: int | None,
    event_type: str,
    description: str,
    explicit_user_id: int | None = None,
    repo: CodeRepo | None = None,
    repos: Sequence[CodeRepo] | None = None,
) -> None:
    user_ids = resolve_learning_event_user_ids(
        db,
        explicit_user_id=explicit_user_id,
        repo=repo,
        repos=repos,
    )
    for owner_id in user_ids:
        db.add(
            CodeLearningEvent(
                user_id=owner_id,
                repo_id=repo_id,
                event_type=event_type,
                description=description,
            )
        )
    try:
        db.commit()
    except Exception:
        db.rollback()


def learning_event_visibility_clause(
    *,
    user_id: int | None,
    repo_ids: Sequence[int] | None,
):
    if user_id is None:
        return None

    normalized_repo_ids = [int(repo_id) for repo_id in (repo_ids or []) if repo_id is not None]
    clauses = [CodeLearningEvent.user_id == int(user_id)]
    if normalized_repo_ids:
        clauses.append(
            and_(
                CodeLearningEvent.user_id.is_(None),
                CodeLearningEvent.repo_id.in_(normalized_repo_ids),
            )
        )
    clauses.append(
        and_(
            CodeLearningEvent.user_id.is_(None),
            CodeLearningEvent.repo_id.is_(None),
            CodeLearningEvent.event_type.in_(("cycle", "error")),
        )
    )
    return or_(*clauses)
