"""brain_batch_jobs persistence helpers."""
from __future__ import annotations

from app.services.trading.batch_job_constants import JOB_CRYPTO_BREAKOUT_SCANNER
from app.services.trading.brain_batch_job_log import (
    batch_job_summary,
    brain_batch_job_record_completed,
    fetch_batch_jobs_page,
    fetch_latest_ok_payload,
)


def test_record_completed_and_fetch_payload(db):
    jid = brain_batch_job_record_completed(
        db,
        JOB_CRYPTO_BREAKOUT_SCANNER,
        ok=True,
        meta={"n": 1},
        payload_json={"results": [{"ticker": "BTC-USD", "score": 7}], "total_scanned": 1},
    )
    db.commit()
    assert len(jid) == 36

    payload, ended_at, meta = fetch_latest_ok_payload(db, JOB_CRYPTO_BREAKOUT_SCANNER)
    assert payload is not None
    assert payload["results"][0]["ticker"] == "BTC-USD"
    assert meta == {"n": 1}
    assert ended_at is not None

    rows, total = fetch_batch_jobs_page(db, limit=10, job_type=JOB_CRYPTO_BREAKOUT_SCANNER)
    assert total >= 1
    assert any(r.id == jid for r in rows)


def test_get_crypto_breakout_cache_reads_postgres(db):
    from app.services.trading.scanner import get_crypto_breakout_cache

    brain_batch_job_record_completed(
        db,
        JOB_CRYPTO_BREAKOUT_SCANNER,
        ok=True,
        meta={},
        payload_json={
            "results": [{"ticker": "ETH-USD", "score": 6}],
            "total_scanned": 5,
            "scan_time": "2026-01-01T00:00:00",
            "elapsed_s": 1.0,
            "errors": 0,
        },
    )
    db.commit()
    cache = get_crypto_breakout_cache()
    assert len(cache["results"]) == 1
    assert cache["results"][0]["ticker"] == "ETH-USD"
    assert cache["total_scanned"] == 5


def test_batch_job_summary(db):
    brain_batch_job_record_completed(
        db,
        "test_job_type_xyz",
        ok=True,
        meta={},
        payload_json={"x": 1},
    )
    db.commit()
    summ = batch_job_summary(db, hours=1)
    types = {s["job_type"] for s in summ}
    assert "test_job_type_xyz" in types
