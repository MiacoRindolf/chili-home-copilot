"""Massive WS auth handshake (2026-08-18 all-day outage).

Ang socket ay bumabati ng hiwalay na {"status":"connected"} frame BAGO ang
auth ack. Ang lumang single-recv handshake ay nabasa ang greeting, nag-deklara
ng "not acknowledged", at nag-reconnect-loop bawat ~2.5s buong araw (32,203
sub-minute connections sa provider dashboard) habang AYOS ang key at
entitlement. Ang handshake ay dapat mag-scan ng bounded na bilang ng frames.
"""
from __future__ import annotations

import json

import pytest

from app.services.massive_client import MassiveWSClient


class _FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def recv(self):
        if not self._frames:
            raise AssertionError("handshake read past the scripted frames")
        return self._frames.pop(0)


def _client(frames):
    c = MassiveWSClient()
    c._ws = _FakeWS(frames)
    c._connection_generation = 7
    return c


def test_separate_greeting_then_ack_authenticates():
    c = _client([
        json.dumps([{"ev": "status", "status": "connected", "message": "Connected Successfully"}]),
        json.dumps([{"ev": "status", "status": "auth_success", "message": "authenticated"}]),
    ])
    c._authenticate()
    assert c._authenticated_generation == 7


def test_coalesced_greeting_and_ack_still_authenticates():
    # Ang dating server behavior (isang frame, dalawang row) ay dapat gumana pa rin.
    c = _client([
        json.dumps([
            {"ev": "status", "status": "connected"},
            {"ev": "status", "status": "auth_success"},
        ]),
    ])
    c._authenticate()
    assert c._authenticated_generation == 7


def test_greetings_only_is_not_acknowledged():
    frames = [json.dumps([{"ev": "status", "status": "connected"}])] * (
        MassiveWSClient._AUTH_HANDSHAKE_MAX_FRAMES
    )
    c = _client(frames)
    with pytest.raises(RuntimeError, match="not acknowledged"):
        c._authenticate()
    assert c._authenticated_generation is None


def test_explicit_reject_raises_rejected():
    c = _client([
        json.dumps([{"ev": "status", "status": "connected"}]),
        json.dumps([{"ev": "status", "status": "auth_failed", "message": "bad key"}]),
    ])
    with pytest.raises(RuntimeError, match="rejected"):
        c._authenticate()
    assert c._authenticated_generation is None


def test_malformed_frame_raises_malformed():
    c = _client(["hindi-json{{{"])
    with pytest.raises(RuntimeError, match="malformed"):
        c._authenticate()
