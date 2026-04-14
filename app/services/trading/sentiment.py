"""Lightweight news sentiment scoring using VADER."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_analyzer = None
_finbert = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            _analyzer = SentimentIntensityAnalyzer()
        except ImportError:
            log.warning("vaderSentiment not installed – sentiment scoring disabled")
    return _analyzer


def _get_finbert():
    """Lazy-load optional FinBERT pipeline (falls back to VADER when unavailable)."""
    global _finbert
    if _finbert is None:
        try:
            from transformers import pipeline

            _finbert = pipeline(
                "sentiment-analysis",
                model="ProsusAI/finbert",
                tokenizer="ProsusAI/finbert",
            )
        except Exception:
            _finbert = False
    return _finbert if _finbert is not False else None


def score_news_sentiment(title: str) -> dict[str, Any]:
    """Score a single headline. Returns {label, score}."""
    if not title:
        return {"label": "neutral", "score": 0.0}
    finbert = _get_finbert()
    if finbert:
        try:
            out = finbert(title[:512])[0]
            lbl = str(out.get("label", "")).strip().lower()
            conf = float(out.get("score", 0.0) or 0.0)
            if lbl == "positive":
                return {"label": "bullish", "score": round(conf, 4)}
            if lbl == "negative":
                return {"label": "bearish", "score": round(-conf, 4)}
            return {"label": "neutral", "score": 0.0}
        except Exception:
            pass
    analyzer = _get_analyzer()
    if not analyzer:
        return {"label": "neutral", "score": 0.0}
    scores = analyzer.polarity_scores(title)
    compound = scores["compound"]
    if compound >= 0.15:
        label = "bullish"
    elif compound <= -0.15:
        label = "bearish"
    else:
        label = "neutral"
    return {"label": label, "score": round(compound, 4)}


def score_news_batch(titles: list[str]) -> list[dict[str, Any]]:
    """Score multiple headlines."""
    return [score_news_sentiment(t) for t in titles]


def aggregate_sentiment(titles: list[str]) -> dict[str, Any]:
    """Compute average sentiment across a list of headlines."""
    if not titles:
        return {"avg_score": 0.0, "label": "neutral", "count": 0}
    results = score_news_batch(titles)
    scores = [r["score"] for r in results]
    avg = sum(scores) / len(scores)
    if avg >= 0.1:
        label = "bullish"
    elif avg <= -0.1:
        label = "bearish"
    else:
        label = "neutral"
    return {"avg_score": round(avg, 4), "label": label, "count": len(titles)}
