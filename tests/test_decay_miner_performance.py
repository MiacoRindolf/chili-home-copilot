from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.fast_path.decay_miner import FastPathDecayMiner


class _FakeListenConn:
    def __init__(self) -> None:
        self.notifies = [SimpleNamespace(payload=str(idx)) for idx in range(5)]

    def fileno(self) -> int:
        return 0

    def poll(self) -> None:
        return None


def test_poll_listen_blocking_drains_notifies_without_front_pops(monkeypatch) -> None:
    conn = _FakeListenConn()
    miner = FastPathDecayMiner.__new__(FastPathDecayMiner)
    miner._listen_conn = conn

    monkeypatch.setattr(
        "app.services.trading.fast_path.decay_miner.select.select",
        lambda *_args, **_kwargs: ([conn], [], []),
    )

    out = miner._poll_listen_blocking(0.0)

    assert [item.payload for item in out] == ["0", "1", "2", "3", "4"]
    assert conn.notifies == []
