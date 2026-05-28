from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from ...models import (
    ChatMessage,
    ReasoningInterest,
    Trade,
)


def _bump_interest(
    db: Session,
    user_id: int,
    topic: str,
    category: str,
    weight_delta: float,
    source: str,
    related_topics: Optional[list[str]] = None,
) -> None:
    _bump_interests(db, user_id, [(topic, category, weight_delta, source, related_topics)])


def _bump_interests(
    db: Session,
    user_id: int,
    updates: Iterable[tuple[str, str, float, str, Optional[list[str]]]],
) -> None:
    normalized = [
        (topic.strip(), category, weight_delta, source, related_topics)
        for topic, category, weight_delta, source, related_topics in updates
        if topic.strip()
    ]
    if not normalized:
        return

    topics = sorted({topic for topic, *_rest in normalized})
    rows = (
        db.query(ReasoningInterest)
        .filter(
            ReasoningInterest.user_id == user_id,
            ReasoningInterest.topic.in_(topics),
        )
        .all()
    )
    existing_by_topic = {row.topic: row for row in rows}
    now = datetime.utcnow()

    for topic, category, weight_delta, source, related_topics in normalized:
        row = existing_by_topic.get(topic)
        if row:
            row.weight = max(0.0, (row.weight or 0.0) + weight_delta)
            row.category = category or row.category
            row.source = source or row.source
            row.last_seen = now
            if related_topics:
                row.related_topics = json.dumps(related_topics)
        else:
            row = ReasoningInterest(
                user_id=user_id,
                topic=topic,
                category=category,
                weight=max(0.0, weight_delta),
                source=source,
                related_topics=json.dumps(related_topics or []),
                last_seen=now,
                created_at=now,
                active=True,
            )
            existing_by_topic[topic] = row
            db.add(row)


def rebuild_interest_graph(db: Session, user_id: int) -> None:
    """Rebuild / refresh the interest graph from chat + trades.

    Simple heuristic:
    - Chat: count top nouns/keywords from recent messages (cheap tokenization)
    - Trades: tickers and sectors from Trade table
    """
    # Soft decay existing interests
    for row in db.query(ReasoningInterest).filter(ReasoningInterest.user_id == user_id):
        row.weight *= 0.9

    # Chat-derived topics (very lightweight keyword heuristic)
    recent_msgs: Iterable[ChatMessage] = (
        db.query(ChatMessage)
        .filter(ChatMessage.convo_key == f"user:{user_id}")
        .order_by(ChatMessage.id.desc())
        .limit(80)
        .all()
    )
    words: Counter[str] = Counter()
    for m in recent_msgs:
        txt = (m.content or "").lower()
        for token in txt.replace(",", " ").replace(".", " ").split():
            token = token.strip("#@ ")
            if len(token) < 4:
                continue
            if token.startswith(("http://", "https://")):
                continue
            words[token] += 1
    updates: list[tuple[str, str, float, str, Optional[list[str]]]] = [
        (word, "explicit", 0.5 + 0.2 * count, "chat", None)
        for word, count in words.most_common(40)
    ]

    # Trading-derived topics (tickers)
    trades: Iterable[Trade] = (
        db.query(Trade)
        .filter(Trade.user_id == user_id)
        .order_by(Trade.opened_at.desc().nullslast())
        .limit(200)
        .all()
    )
    ticker_counts: Counter[str] = Counter()
    for t in trades:
        if t.ticker:
            ticker_counts[t.ticker.upper()] += 1
    updates.extend(
        (ticker, "inferred_trading", 1.0 + 0.3 * count, "trading", None)
        for ticker, count in ticker_counts.most_common(50)
    )
    _bump_interests(db, user_id, updates)

    db.commit()


def get_top_interests(db: Session, user_id: int, limit: int = 20) -> list[dict]:
    rows = (
        db.query(ReasoningInterest)
        .filter(ReasoningInterest.user_id == user_id, ReasoningInterest.active.is_(True))
        .order_by(ReasoningInterest.weight.desc())
        .limit(limit)
        .all()
    )
    result: list[dict] = []
    for r in rows:
        result.append(
            {
                "topic": r.topic,
                "category": r.category,
                "weight": float(r.weight or 0.0),
                "source": r.source,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
        )
    return result
