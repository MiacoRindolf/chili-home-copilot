"""PRINT-INDEXED PULLBACK→NEW-HIGH CYCLES — ang bilang ng pagod ng tape ([62], kasama ang [61]).

Obserbasyon ng operator (2026-09-10 23:20Z): "marami ring talo kasi nag-enter sa backside
after tuloy-tuloy na successful pullbacks". Ang tanong ay HINDI "front side ba o back side"
(sinasagot iyon ng VWAP bench, at barya ang binibili nito — [56]: 15,288 pagtanggi, 49.6%
up-rate, 21 minutong lipas na session-VWAP). Ang tanong ay KAILAN TUMITIGIL ANG SERYE: 83% ng
mga spike ay gumagawa ng bagong high kahit pagkatapos ng BUONG retrace ([53] sukat 3), kaya
walang saysay ang "malalim ang pullback ⇒ tapos na". Ang sagot ay nasa TAPE: ilang kumpletong
pullback→bagong-high cycle na ang nakaraan, at LUMILIIT ba ang bawat sunod na spike
(amplitude, print rate, buy share) kumpara sa nauna.

Doktrina (PROGRAM_BRIEF): ang tick ang laging sumasagot. Walang orasan dito — walang "N
segundo", walang bar, walang quote-mid. Bawat estado ay galing sa PRINTS (``iqfeed_trade_ticks``)
at ang bawat window ay bilang ng print. Ang resulta ay CONDITIONING (size/priority), hindi
kailanman binary veto: ang pinakapagod na tape ay pumapasok pa rin sa ``floor`` ng laki.

Kahulugan (dalisay, print-indexed, self-scaled):
  * onset low  = ang pinakamababang print bago ang unang bagong-high na takbo ng tape ng araw.
                 Nag-uulit ito pababa habang WALANG pang natapos na cycle (ang tape ay
                 nagsisimula sa gitna ng galaw — tingnan ang [59]: median na unang tick 13:57Z).
  * spike k    = takbo mula sa spike-low L_k hanggang sa running high H_k. amp_k = H_k − L_k.
  * pullback   = umatras ang presyo ng >= ``pullback_frac`` × amp_k mula sa H_k — praksyon ng
                 SARILING amplitude ng spike (self-scaled; walang sentimo, walang porsyento).
  * cycle k    ay NATATAPOS kapag, pagkatapos ng gayong pullback, may print na lumagpas sa H_k.
                 Ang low ng pullback ang nagiging L_{k+1}. Ang BUONG retrace (pb_low <= L_k)
                 ay BILANG pa rin — iyon mismo ang 83% na sinusukat ng [53].
  * cycle index sa oras t = ilang cycle ang natapos hanggang t.

Ang lahat ay INCREMENTAL at O(1) kada print: ang runner ay nagpapakain ng bagong prints kada
tick at ang estado ay JSON (``to_dict``/``from_dict``) sa ``live_exec`` — symbol-day state ito,
kaya HINDI ito kasama sa ``_RECYCLE_ENTRY_STATE_KEYS``.

``feed()`` ay hindi kailanman nag-raise: ang basag na hilera ay nilalaktawan, ang scanner ay
nananatiling gumagana (fail-open ⇒ mult 1.0 na may pangalang dahilan).

ANG PRESYONG ISINUSUKAT (refuter, 2026-09-11): ang ``cycle_features_at`` ay tumatanggap ng
presyo mula sa caller, at ang caller sa live (``live_runner._cycle_exhaustion_conditioning``)
ay nagbibigay ng ``scanner.last_px`` — ang HULING PRINT — hindi ng quote-mid. Ganoon din ang
sukat sa ``scripts/cycle_exhaustion_replay_62.py``. Kaya totoo ang "walang quote-mid" sa itaas
sa buong daan, hindi lamang sa loob ng modyul.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── ANG SINUKAT NA DISTRIBUSYON ───────────────────────────────────────────────
# Populasyon: ang 81 live Alpaca leg 2026-08-27 22:37Z .. 2026-09-10 22:24Z (40 symbol-day,
# buong-araw na tape 04:00–16:00 ET bawat isa, binasa sa 20-minutong chunk) + bawat kumpletong
# cycle ng parehong 40 symbol-day. Ang mga numero ay nasa PR body at sa planner row [62];
# WALANG isa rito ang pinili in-sample — ang bawat isa ay iniuulat sa resibo bilang `binding`.
#
# Ang bawat termino ay pumapasok sa score kapag ang CLUSTERED AUC nito (mean ng per-symbol-day
# AUC) ay nasa labas ng 0.40/0.60; ang `sign` ay ang sinukat na direksyon (+1 = mas mataas ang
# halaga ⇒ MAS pagod; −1 = mas mababa ⇒ mas pagod). Ang `q_lo`/`q_hi` ay ang p10/p90 ng
# sinukat na distribusyon ng termino: doon ito na-normalize sa [0,1] bago i-average.
MEASURED_DERIVATION = (
    "cycle_index_62 — 81 live Alpaca legs / 40 symbol-days / 3,124 armed sessions without "
    "entry, 2026-08-27..2026-09-10; per-completed-cycle population of the same 40 days; "
    "scored at the DECISION instant (live_entry_submitted − place_profile_ms.total) on the "
    "LAST PRINT price, with the full 4-term set"
)

# ── TERM SPEC ────────────────────────────────────────────────────────────────
# name -> (sign, q_lo, q_hi, clustered_auc)
CycleTerm = tuple[float, float, float, float]

# ANG PRAKSYON NG PULLBACK. Sinukat sa 0.25 AT 0.50 sa parehong 40 symbol-day:
#   0.25 -> 703 kumpletong cycle; cycle index sa entry p10 3 / p50 16 / p90 41 — 69 sa 81 leg
#           (85%) ang nahuhulog sa IISANG bucket (6+), kaya walang masasakyang spread.
#   0.50 -> 201 kumpletong cycle; cycle index sa entry p10 3 / p25 4 / p50 5 / p75 7 / p90 10,
#           at ang per-cycle exhaustion signature ay MAS MALINAW (clustered AUC ng amp_pct
#           0.057 kumpara sa 0.245; prints 0.111 kumpara sa 0.232) — ang huling high ng araw ay
#           malaki at mabagal na blow-off (amp_pct p50 15.34% sa bigong cycle vs 3.21% sa
#           matagumpay).
# Ang PAGPILI ng praksyon ay HINDI pumipili ng termino: ang bawat termino sa ibaba ay
# kailangang pumasa sa PAREHONG 0.25 at 0.50 (tingnan ang panuntunan sa CYCLE_EXHAUSTION_TERMS).
CYCLE_PULLBACK_FRAC_BASE = 0.50

# Hangganan ng nakatagong ledger sa snapshot JSON (INFRA na hangganan, hindi hango). Ang
# `cycle_index` ay HIWALAY na counter kaya walang impormasyong nawawala para sa [62]; ang
# features ay bumabasa LAMANG ng huling natapos na cycle + amp0. Ang orihinal na sukat (p25 4 /
# p50 5 / p90 10.4, max 11 sa 34 symbol-day) ay nagsabing "hindi kailanman pinuputol ang isang
# tunay na araw" — ⚠️ MALI ang premise na iyon (review ng #1419, 2026-09-11): sa 14-d na sample
# ng [65] replay, 4/81 leg ay may n_cycles 17 SA FILL (naputol ang pinakalumang cycle), at ang
# TNON 2026-09-11 ay may eksaktong 16. Ang pinuputol ay ang PINAKALUMANG row — kaya ang
# populasyon ng `cycles` ay nakadepende sa hangganang ito at sa kung kailan nagsimula ang
# ledger; HINDI ito dapat magpasya ng anuman: ang [65] tick deadman ay binabasa lamang ito
# bilang RESIBO (`cont_context`), at ang resibo ay nag-uulat ng `max_cycles`,
# `cycles_truncated` at `max_cycles_binding`.
CYCLE_LEDGER_MAX_CYCLES = 16

# ── ANG SCORE ────────────────────────────────────────────────────────────────
# PANUNTUNAN SA PAGPILI NG TERMINO (nakasulat bago tingnan ang score, at nasa PR body):
#  (a) clustered AUC (mean ng per-symbol-day AUC, 10 cluster) laban sa CONTINUATION label
#      (bagong HOD sa loob ng 60 min pagkatapos ng entry) ay nasa LABAS ng [0.40, 0.60];
#  (b) ang PAREHONG sign ay lumalabas sa PAREHONG sinukat na praksyon (0.25 at 0.50) — ang
#      `buy_share_delta` (B 0.267 sa 0.25, 0.717 sa 0.50) at `last_amp_ratio` (0.388 / 0.775)
#      ay TINANGGIHAN dito: nagpapalit sila ng sign kasabay ng praksyon, kaya artifact sila ng
#      kahulugan at hindi katangian ng tape;
#  (c) HINDI ito kontradiksyon sa money label (pnl>0): walang terminong nasa labas ng banda sa
#      KABALIGTARANG direksyon doon;
#  (d) HINDI ito monotone-in-time sa loob ng isang symbol-day. Dito NAHUHULOG ang `cycle_index`
#      mismo (clustered AUC 0.025 laban sa continuation) — ang index ay hindi bumababa kailanman,
#      kaya ang isang label na "may bagong HOD sa susunod na 60 min" ay MEKANIKAL na mas bihira
#      sa dulo ng araw; at wala itong money edge (clustered AUC 0.511 sa pnl>0). Iniuulat pa rin
#      ito sa resibo at ibinibigay sa selection ([13]) — hindi lang ito TERMINO ng score.
# Ang q_lo/q_hi ay ang p10/p90 ng SINUKAT na distribusyon ng termino sa 81 leg sa frac 0.50.
# ── ANG RAMP NG LAKI ─────────────────────────────────────────────────────────
# ⚠️ MULING HINANGO SA DESISYONG SANDALI (refuter, 2026-09-11). Ang unang hango ay nag-i-score
# sa `live_entry_filled` — ang tape hanggang sa FILL, sa presyo ng fill. Pero ang laki ay
# napagdedesisyunan BAGO pa ipadala ang order. Sinukat sa mismong populasyon (85 live Alpaca
# leg, 2026-08-27..2026-09-11): `live_entry_pending_place` -> fill = p50 13.76 s (p90 30.24,
# max 97.24); ang desisyon mismo (`live_entry_submitted.ts` bawas ang `place_profile_ms.total`,
# 81/81 leg) -> fill = p10 4.83 / p50 7.96 / p90 14.08 / max 24.86 s. Sa p90 na dating na 11.45
# print/s iyon ay ~90 print na WALA PA noong nagdesisyon — mismong ang mga printong gumagalaw
# ng `pos_in_range`, `cur_buy_share` at `prints_since_high`. Kaya ang lahat ng nasa ibaba ay
# sinukat sa DESISYONG sandali, at sa HULING PRINT bilang presyo (hindi fill, hindi quote-mid)
# — eksaktong dalawang bagay na binabasa ng `_cycle_exhaustion_conditioning` sa live.
#
# Sinukat ng `scripts/cycle_exhaustion_replay_62.py` sa 81 leg gamit ang module na ito (hindi
# kopya — ang mismong scanner na tumatakbo sa runner):
#   cycle_index sa desisyon: p10 3 / p50 5 / p90 10 — 81/81 leg ang may BUONG 4 na termino
#   score quantiles: p10 0.2410 / p25 0.3841 / p50 0.5383 / p75 0.6340 / p90 0.7131
#   tercile (27 leg bawat isa):  low  score_p50 0.2997  sumPnL -416.59  winrate 0.22  cont 0.59
#                                mid  score_p50 0.5383  sumPnL -141.58  winrate 0.26  cont 0.33
#                                high score_p50 0.6660  sumPnL -719.78  winrate 0.30  cont 0.19
# ANG FLOOR: ang unang panukala ay win-rate(high)/win-rate(low) — PINABULAANAN ito ng datos
# (0.2963/0.2222 = 1.3333 ⇒ WALANG size-down ⇒ resibo lamang ang buong mekanismo). Halos patag
# ang win-rate dahil ang SARILI nating exit ang pumuputol sa bawat panalo ([46]: 89% ng 189 na
# natalong leg ay BERDE noong isang sandali). Ang label na pinagpilian ng mga termino ay
# CONTINUATION, at doon MONOTONE ang score: 0.5926 -> 0.3333 -> 0.1852. Ang floor ay ang ratio
# nito — 0.1852/0.5926 = 0.3125 — ibig sabihin, ang pinakapagod na tercile ay binibigyan ng
# laking katumbas ng dalas kung saan IBINIBIGAY PA RIN ng tape ang susunod na bagong high.
# Hindi ito kailanman bumababa sa `chili_momentum_frontside_size_floor` (0.25).
# REPLAY sa 81 leg / 14 araw: -1,277.95 -> -873.31 (+404.64); 40 leg ang na-size-down;
# panalo -167.00, talo +571.64 — BINABAYARAN nito ang mga panalo, hindi libre.
CYCLE_EXHAUSTION_Q50 = 0.5383
CYCLE_EXHAUSTION_Q90 = 0.7131
CYCLE_EXHAUSTION_FLOOR = 0.3125

CYCLE_EXHAUSTION_TERMS: dict[str, CycleTerm] = {
    # pos_in_range: MAS MABABA sa saklaw ng araw = MAS pagod (clustered AUC 0.707 pareho sa
    # 0.25 at 0.50; money label 0.689 — iisa ang direksyon).
    "pos_in_range": (-1.0, 0.457, 0.937, 0.707),
    # ext_x_amp0: kung ilang beses ang UNANG amplitude ang nalayo na sa onset low. Mas
    # malayo = mas pagod (clustered AUC 0.158; money label 0.557 = nasa loob ng banda).
    "ext_x_amp0": (1.0, 1.058, 80.000, 0.158),
    # last_pb_depth_ratio: kung gaano kalalim ang HULING pullback bilang bahagi ng sariling
    # amplitude. MABABAW = pagod (clustered AUC 0.675 sa 0.50, 0.692 sa 0.25) — kabaligtaran
    # ng intuwisyon at kapareho ng [53]: ang MALALIM na retrace ay tumutuloy pa rin.
    "last_pb_depth_ratio": (-1.0, 0.630, 1.769, 0.675),
    # cur_buy_share: Lee-Ready na bahagi ng aggressor-buy sa KASALUKUYANG spike. Mababa =
    # pagod (clustered AUC 0.683 sa 0.50, 0.607 sa 0.25).
    "cur_buy_share": (-1.0, 0.468, 0.545, 0.683),
}


def _f(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _acc_new(ts: float, size: float, buy: float) -> dict[str, float]:
    return {"n": 1.0, "t0": ts, "t1": ts, "buy": buy, "vol": size}


def _acc_add(acc: dict[str, float], ts: float, size: float, buy: float) -> None:
    acc["n"] += 1.0
    acc["t1"] = ts
    acc["buy"] += buy
    acc["vol"] += size


def _acc_stats(acc: Mapping[str, float] | None) -> dict[str, float | None]:
    """prints / secs / print-rate / Lee-Ready buy share ng isang akumulador."""
    if not acc:
        return {"prints": None, "secs": None, "rate": None, "buy_share": None}
    n = float(acc.get("n") or 0.0)
    secs = max(1e-6, float(acc.get("t1") or 0.0) - float(acc.get("t0") or 0.0))
    vol = float(acc.get("vol") or 0.0)
    return {
        "prints": n,
        "secs": secs,
        "rate": (n / secs) if secs > 0 else None,
        "buy_share": (float(acc.get("buy") or 0.0) / vol) if vol > 0 else None,
    }


class PullbackCycleScanner:
    """Incremental, O(1)-kada-print na state machine sa ibabaw ng tape ng ISANG symbol-day.

    Ang hilera ay ``(observed_at, id, price, size, bid, ask)``. Ang ``observed_at`` ay
    ginagamit LAMANG bilang stamp ng cursor at para sa print-RATE ratio (ratio ng dalawang
    sinukat na rate — hindi ito threshold na orasan). Walang wall-clock na desisyon dito.
    """

    __slots__ = (
        "pullback_frac",
        "max_cycles",
        "n_prints",
        "n_cycles",
        "cycles",
        "hod",
        "hod_i",
        "spike_low",
        "spike_low_i",
        "cur_low",
        "cur_low_i",
        "in_pullback",
        "onset_low",
        "onset_i",
        "run_lo",
        "run_hi",
        "amp0",
        "last_px",
        "last_sign",
        "last_observed_at",
        "last_id",
        "_spike_acc",
        "_low_acc",
        "_high_acc",
    )

    # Hangganan ng ledger (ang cycle INDEX ay hiwalay na counter kaya walang impormasyong
    # nawawala). IISA ito ng `CYCLE_LEDGER_MAX_CYCLES` — dating 64 dito at 16 doon, at ang
    # `from_dict` ay bumabagsak sa DEFAULT kapag walang `max_cycles` sa naka-persist na dict,
    # kaya ang bahagyang naisulat na estado ay tahimik na nagdadala ng 4x na ledger sa
    # per-tick na snapshot JSON. Isang pinagmulan lang ng halaga (refuter, 2026-09-11).
    DEFAULT_MAX_CYCLES = CYCLE_LEDGER_MAX_CYCLES

    def __init__(self, pullback_frac: float, *, max_cycles: int = DEFAULT_MAX_CYCLES) -> None:
        pf = _f(pullback_frac)
        self.pullback_frac: float = pf if (pf is not None and 0.0 < pf < 1.0) else 0.5
        self.max_cycles: int = max(1, int(max_cycles or self.DEFAULT_MAX_CYCLES))
        self.n_prints: int = 0
        self.n_cycles: int = 0
        self.cycles: list[dict[str, Any]] = []
        self.hod: float | None = None
        self.hod_i: int = 0
        self.spike_low: float | None = None
        self.spike_low_i: int = 0
        self.cur_low: float | None = None
        self.cur_low_i: int = 0
        self.in_pullback: bool = False
        self.onset_low: float | None = None
        self.onset_i: int = 0
        self.run_lo: float | None = None
        self.run_hi: float | None = None
        self.amp0: float | None = None
        self.last_px: float | None = None
        self.last_sign: int = 0
        self.last_observed_at: str | None = None
        self.last_id: int = 0
        self._spike_acc: dict[str, float] | None = None
        self._low_acc: dict[str, float] | None = None
        self._high_acc: dict[str, float] | None = None

    # ── PAGPAPAKAIN ──────────────────────────────────────────────────────────
    def feed(self, rows: Iterable[Sequence[Any]]) -> int:
        """Kumain ng mga bagong print (ASCENDING sa (observed_at, id)). Ibinabalik ang bilang
        ng natanggap. HINDI KAILANMAN NAG-RAISE."""
        taken = 0
        try:
            for r in rows:
                try:
                    if self._feed_one(r):
                        taken += 1
                except Exception:  # isang basag na hilera ay hindi pumapatay ng scanner
                    continue
        except Exception:
            logger.debug("[tape_cycles] feed aborted mid-stream", exc_info=True)
        return taken

    def _feed_one(self, r: Sequence[Any]) -> bool:
        observed_at = r[0]
        rid = r[1]
        px = _f(r[2])
        if px is None or px <= 0.0:
            return False
        size = _f(r[3]) or 0.0
        bid = _f(r[4])
        ask = _f(r[5])
        ts = _epoch(observed_at)
        if ts is None:
            return False

        # Lee-Ready quote rule, tick-rule fallback (zero-tick ay nagdadala ng naunang sign) —
        # KAPAREHO ng production `_signed_tape_features` sa entry_gates.py.
        sign = 0
        if bid is not None and ask is not None and ask > bid > 0.0:
            mid = (ask + bid) / 2.0
            sign = 1 if px >= ask else (-1 if px <= bid else (1 if px > mid else (-1 if px < mid else 0)))
        if sign == 0 and self.last_px is not None:
            sign = 1 if px > self.last_px else (-1 if px < self.last_px else self.last_sign)
        self.last_px = px
        self.last_sign = sign
        buy = size if sign > 0 else 0.0

        i = self.n_prints
        self.n_prints = i + 1
        try:
            self.last_id = int(rid or 0)
        except (TypeError, ValueError):
            self.last_id = 0
        self.last_observed_at = _iso(observed_at)

        if self.hod is None:  # unang print ng symbol-day
            self.hod = self.spike_low = self.cur_low = self.onset_low = px
            self.hod_i = self.spike_low_i = self.cur_low_i = self.onset_i = i
            self.run_lo = self.run_hi = px
            self._spike_acc = _acc_new(ts, size, buy)
            self._low_acc = _acc_new(ts, size, buy)
            self._high_acc = dict(self._spike_acc)
            return True

        self.run_lo = px if (self.run_lo is None or px < self.run_lo) else self.run_lo
        self.run_hi = px if (self.run_hi is None or px > self.run_hi) else self.run_hi
        if self._spike_acc is None:
            self._spike_acc = _acc_new(ts, size, buy)
        else:
            _acc_add(self._spike_acc, ts, size, buy)
        if self._low_acc is None:
            self._low_acc = _acc_new(ts, size, buy)
        else:
            _acc_add(self._low_acc, ts, size, buy)

        if px > float(self.hod):
            if self.in_pullback:
                self._close_cycle(i)
            self.hod = px
            self.hod_i = i
            self.cur_low = px
            self.cur_low_i = i
            self._low_acc = _acc_new(ts, size, buy)
            self._high_acc = dict(self._spike_acc)
            return True

        if self.cur_low is None or px < float(self.cur_low):
            self.cur_low = px
            self.cur_low_i = i
            self._low_acc = _acc_new(ts, size, buy)
        if self.n_cycles == 0 and self.onset_low is not None and float(self.cur_low) < float(self.onset_low):
            # WALA pang cycle: ang mas mababang low ay nag-uulit ng onset (nagsimula ang tape
            # sa gitna ng galaw). Pagkatapos ng UNANG cycle ay hindi na ito gumagalaw.
            self.onset_low = float(self.cur_low)
            self.onset_i = self.cur_low_i
            self.spike_low = float(self.cur_low)
            self.spike_low_i = self.cur_low_i
            self.hod = float(self.cur_low)
            self.hod_i = self.cur_low_i
            self.in_pullback = False
            self._spike_acc = dict(self._low_acc or {})
            self._high_acc = dict(self._spike_acc)
        amp = float(self.hod) - float(self.spike_low if self.spike_low is not None else self.hod)
        if not self.in_pullback and amp > 0.0:
            if (float(self.hod) - float(self.cur_low)) >= self.pullback_frac * amp:
                self.in_pullback = True
        return True

    def _close_cycle(self, i: int) -> None:
        hi = float(self.hod or 0.0)
        lo = float(self.spike_low if self.spike_low is not None else hi)
        pb = float(self.cur_low if self.cur_low is not None else hi)
        amp = hi - lo
        st = _acc_stats(self._high_acc)
        rec = {
            "k": self.n_cycles,
            "spike_low": lo,
            "hi": hi,
            "pb_low": pb,
            "amp": amp,
            "prints": st["prints"],
            "secs": st["secs"],
            "rate": st["rate"],
            "buy_share": st["buy_share"],
            "pb_depth_ratio": ((hi - pb) / amp) if amp > 0.0 else None,
            "new_hi_i": i,
        }
        self.cycles.append(rec)
        if len(self.cycles) > self.max_cycles:
            del self.cycles[: len(self.cycles) - self.max_cycles]
        self.n_cycles += 1
        if self.n_cycles == 1 and amp > 0.0:
            self.amp0 = amp
        # Ang bagong spike ay nagsisimula sa LOW ng pullback.
        self.spike_low = pb
        self.spike_low_i = self.cur_low_i
        self.in_pullback = False
        self._spike_acc = dict(self._low_acc or {})

    # ── PERSISTENCE (JSON sa live_exec) ──────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "pullback_frac": self.pullback_frac,
            "max_cycles": self.max_cycles,
            "n_prints": self.n_prints,
            "n_cycles": self.n_cycles,
            "cycles": list(self.cycles),
            "hod": self.hod,
            "hod_i": self.hod_i,
            "spike_low": self.spike_low,
            "spike_low_i": self.spike_low_i,
            "cur_low": self.cur_low,
            "cur_low_i": self.cur_low_i,
            "in_pullback": bool(self.in_pullback),
            "onset_low": self.onset_low,
            "onset_i": self.onset_i,
            "run_lo": self.run_lo,
            "run_hi": self.run_hi,
            "amp0": self.amp0,
            "last_px": self.last_px,
            "last_sign": self.last_sign,
            "last_observed_at": self.last_observed_at,
            "last_id": self.last_id,
            "spike_acc": self._spike_acc,
            "low_acc": self._low_acc,
            "high_acc": self._high_acc,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None, *, pullback_frac: float | None = None) -> "PullbackCycleScanner":
        frac = _f(pullback_frac)
        if frac is None and isinstance(d, Mapping):
            frac = _f(d.get("pullback_frac"))
        sc = cls(frac if frac is not None else 0.5, max_cycles=int((d or {}).get("max_cycles") or cls.DEFAULT_MAX_CYCLES))
        if not isinstance(d, Mapping):
            return sc
        try:
            sc.n_prints = int(d.get("n_prints") or 0)
            sc.n_cycles = int(d.get("n_cycles") or 0)
            cyc = d.get("cycles")
            sc.cycles = [dict(c) for c in cyc if isinstance(c, Mapping)] if isinstance(cyc, list) else []
            sc.hod = _f(d.get("hod"))
            sc.hod_i = int(d.get("hod_i") or 0)
            sc.spike_low = _f(d.get("spike_low"))
            sc.spike_low_i = int(d.get("spike_low_i") or 0)
            sc.cur_low = _f(d.get("cur_low"))
            sc.cur_low_i = int(d.get("cur_low_i") or 0)
            sc.in_pullback = bool(d.get("in_pullback"))
            sc.onset_low = _f(d.get("onset_low"))
            sc.onset_i = int(d.get("onset_i") or 0)
            sc.run_lo = _f(d.get("run_lo"))
            sc.run_hi = _f(d.get("run_hi"))
            sc.amp0 = _f(d.get("amp0"))
            sc.last_px = _f(d.get("last_px"))
            sc.last_sign = int(d.get("last_sign") or 0)
            lo = d.get("last_observed_at")
            sc.last_observed_at = str(lo) if lo else None
            sc.last_id = int(d.get("last_id") or 0)
            sc._spike_acc = _acc_or_none(d.get("spike_acc"))
            sc._low_acc = _acc_or_none(d.get("low_acc"))
            sc._high_acc = _acc_or_none(d.get("high_acc"))
        except Exception:
            logger.debug("[tape_cycles] from_dict partial restore", exc_info=True)
        return sc


def _acc_or_none(v: object) -> dict[str, float] | None:
    if not isinstance(v, Mapping):
        return None
    out: dict[str, float] = {}
    for k in ("n", "t0", "t1", "buy", "vol"):
        f = _f(v.get(k))
        if f is None:
            return None
        out[k] = f
    return out


def _epoch(observed_at: Any) -> float | None:
    try:
        return float(observed_at.timestamp())  # type: ignore[union-attr]
    except Exception:
        pass
    f = _f(observed_at)
    return f


def _iso(observed_at: Any) -> str | None:
    try:
        return observed_at.isoformat()  # type: ignore[union-attr]
    except Exception:
        return str(observed_at) if observed_at is not None else None


# ── FEATURES SA ISANG DESISYONG SANDALI ──────────────────────────────────────
def cycle_features_at(scanner: PullbackCycleScanner | None, price: object) -> dict[str, Any]:
    """Ang estado ng tape sa sandaling ito, walang orasan. Lahat ay self-scaled na ratio.

    Ibinabalik ang ``{}`` kapag walang scanner / walang print pa (⇒ tinatrato ng caller bilang
    ``no_tape_state``: mult 1.0, pangalang fallback, hindi tahimik).
    """
    if scanner is None or scanner.n_prints <= 0 or scanner.hod is None:
        return {}
    px = _f(price)
    cur = _acc_stats(scanner._spike_acc)  # noqa: SLF001 — pareho itong module
    last = scanner.cycles[-1] if scanner.cycles else None
    hi = float(scanner.hod)
    sl = float(scanner.spike_low if scanner.spike_low is not None else hi)
    cur_amp = hi - sl
    out: dict[str, Any] = {
        "cycle_index": int(scanner.n_cycles),
        "in_pullback": bool(scanner.in_pullback),
        "n_prints": int(scanner.n_prints),
        "prints_since_high": int(max(0, scanner.n_prints - 1 - int(scanner.hod_i))),
        "cur_amp": cur_amp,
        "cur_buy_share": cur["buy_share"],
        "pos_in_range": None,
        "ext_x_amp0": None,
        "amp_ratio": None,
        "rate_ratio": None,
        "buy_share_delta": None,
        "last_pb_depth_ratio": (last or {}).get("pb_depth_ratio"),
    }
    if px is not None and scanner.run_hi is not None and scanner.run_lo is not None:
        span = float(scanner.run_hi) - float(scanner.run_lo)
        if span > 0.0:
            out["pos_in_range"] = (px - float(scanner.run_lo)) / span
    if px is not None and scanner.onset_low is not None and scanner.amp0 and float(scanner.amp0) > 0.0:
        out["ext_x_amp0"] = (px - float(scanner.onset_low)) / float(scanner.amp0)
    if last:
        la = _f(last.get("amp"))
        lr = _f(last.get("rate"))
        lb = _f(last.get("buy_share"))
        if la is not None and la > 0.0:
            out["amp_ratio"] = cur_amp / la
        if lr is not None and lr > 0.0 and cur["rate"] is not None:
            out["rate_ratio"] = float(cur["rate"]) / lr
        if lb is not None and cur["buy_share"] is not None:
            out["buy_share_delta"] = float(cur["buy_share"]) - lb
    return out


# ── ANG EXHAUSTION SCORE ─────────────────────────────────────────────────────
def _unit(v: float, q_lo: float, q_hi: float) -> float:
    if q_hi == q_lo:
        return 0.5
    return min(1.0, max(0.0, (v - q_lo) / (q_hi - q_lo)))


def cycle_exhaustion_score(
    feats: Mapping[str, Any] | None,
    terms: Mapping[str, CycleTerm],
    *,
    min_terms: int | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Rank-average ng mga terminong SINUKAT na may signal (clustered AUC sa labas ng 0.40/0.60).

    Ang bawat termino ay kino-convert sa sariling quantile laban sa iniulat na distribusyon
    (p10→0, p90→1), pinipihit ng sinukat na `sign`, at pinag-a-average. Walang bigat na
    pinili — pantay ang lahat (rank-average), kaya walang in-sample fitting.

    ``min_terms`` ang PINAKAMALIIT na bilang ng terminong kailangan bago tumawag ng score;
    ang default ay LAHAT ng termino, dahil doon sinukat ang q50/q90/floor. Ang ibang bilang
    ay ibang distribusyon — hindi ito kayang i-average pabalik (tingnan sa ibaba).

    Ibinabalik ang ``(score|None, detail)``. ``None`` kapag kulang ang mababasang termino
    (⇒ mult 1.0 na may pangalang dahilan sa resibo).
    """
    if not isinstance(feats, Mapping) or not terms:
        return None, {"reason": "no_features"}
    parts: dict[str, float] = {}
    for name, spec in terms.items():
        try:
            sign, q_lo, q_hi, _auc = spec
        except Exception:
            continue
        v = _f(feats.get(name))
        if v is None:
            continue
        u = _unit(v, float(q_lo), float(q_hi))
        parts[name] = u if float(sign) >= 0 else (1.0 - u)
    need = len(terms) if min_terms is None else max(0, int(min_terms))
    if len(parts) < need:
        # ANG BUTAS NA INAYOS (refuter, 2026-09-11): ang dating `sum/len` sa KUNG ANO ANG
        # MABABASA ay nagbabalik ng score kahit isang termino lang. Pero ang `ext_x_amp0` at
        # `last_pb_depth_ratio` ay STRUKTURAL na None hanggang matapos ang UNANG cycle, at ang
        # `ext_x_amp0` (q_hi 80.0) ay halos laging ~0.008 ang ambag — kaya ang pagkawala nito
        # ay naghahatak ng 4-terminong score pataas ng ~0.25, na 1.5x ng buong lapad ng ramp
        # (0.7131 − 0.5383 = 0.1748). Resulta: ang tape na may ZERO kumpletong cycle — ang
        # PINAKASARIWA — ay tumatama sa floor (0.3125) habang ang 5-cycle na tape ay 0.6978.
        # Kabaligtaran iyon ng sinusukat. Ang distribusyon (q50/q90/floor) ay sinukat sa
        # KUMPLETONG hanay ng termino, kaya doon LAMANG ito may bisa: kulang ⇒ None ⇒ mult 1.0
        # na may PANGALANG dahilan sa resibo (hindi katahimikan).
        return None, {
            "reason": "insufficient_terms",
            "n_terms": len(parts),
            "min_terms": need,
            "missing": sorted(set(terms) - set(parts)),
            "terms": {k: round(v, 4) for k, v in parts.items()},
        }
    score = sum(parts.values()) / float(len(parts))
    return score, {"terms": {k: round(v, 4) for k, v in parts.items()}, "n_terms": len(parts)}


def cycle_exhaustion_size_multiplier(
    score: float | None,
    *,
    floor: float,
    q50: float,
    q90: float,
) -> tuple[float, dict[str, Any] | None]:
    """SIZE-DOWN LANG. 1.0 sa/pababa ng ``q50`` ng sinukat na score, linear pababa sa ``floor``
    sa ``q90``, at ``floor`` sa itaas noon. HINDI KAILANMAN veto (floor > 0) at hindi kailanman
    nagpapalaki (<= 1.0) — conditioning, hindi binary.

    Fail-open: walang score / basag na quantile ⇒ ``(1.0, None)``.
    """
    s = _f(score)
    fl = _f(floor)
    lo = _f(q50)
    hi = _f(q90)
    if s is None or fl is None or lo is None or hi is None:
        return 1.0, None
    fl = min(1.0, max(0.0, fl))
    if fl >= 1.0 or not (hi > lo):
        return 1.0, None
    if s <= lo:
        return 1.0, {"mult": 1.0, "score": round(s, 4), "band": "at_or_below_p50"}
    frac = min(1.0, (s - lo) / (hi - lo))
    mult = 1.0 - frac * (1.0 - fl)
    mult = min(1.0, max(fl, mult))
    return mult, {
        "mult": round(mult, 4),
        "score": round(s, 4),
        "band": "ramp" if s < hi else "at_floor",
        "q50": round(lo, 4),
        "q90": round(hi, 4),
        "floor": round(fl, 4),
    }


# ── ANG TANGING DAANAN PAPUNTA SA DB (hiwalay sa dalisay na core sa itaas) ────
def _asof_default(as_of: Any) -> Any:
    """Kaparehong replay-aware na chokepoint na ginagamit ng bawat tape read
    (entry_gates._tape_asof_default): LIVE ⇒ naive wall-UTC; FSM REPLAY ⇒ ang sim clock."""
    if as_of is not None:
        return as_of
    try:
        from .live_runner import _utcnow as _lr_utcnow

        return _lr_utcnow()
    except Exception:
        from datetime import datetime as _dt

        return _dt.utcnow()


# Ilang BOUNDED na pagbasa ang ginagawa sa loob ng ISANG tick habang nasa likod pa ang ledger.
# DERIVATION: ang buong-araw na tape ay p90 346,769 print (max 560,806) sa 40 symbol-day; sa
# default na 5,000 kada pagbasa, ang 8 pagbasa kada tick ay 40,000 print/tick, kaya ang p90 na
# araw ay naaabutan sa <= 9 tick at ang pinakamabigat sa <= 15 — ilang segundo lang matapos
# magsimulang mag-tick ang sesyon, at hindi ang minuto-minutong pagkakapit na ibinubunga ng
# isang pagbasa kada tick. Pagkatapos maabutan, ang steady state ay p90 11.45 print/segundo,
# kaya ISANG maliit na pagbasa na lang kada tick. Hindi ito threshold — hangganan ito ng gawain.
# SINUKAT NA GASTOS — MAINIT LAMANG ang dating numero (4.9 ms kada pagbasa, ~40 ms para sa 8).
# MULING SINUKAT NANG MALAMIG ([66] review, 2026-09-11 18:25Z, buhay na DB, ang ipinadalang
# fence, LIMIT 5000, EXPLAIN (ANALYZE, BUFFERS), 13 malamig na pagbasa sa 10 simbolo / 3 araw):
# malamig p50 1,231.7 ms / p90 1,621.7 / max 1,763.5 (read 731–2,439 page — ~2 hilera kada heap
# page, kaya ang gastos ay ang random na heap fetch, hindi ang plano); ang PAREHONG pagbasa
# pagkatapos ay mainit p50 10.3 ms / max 22.4 (15 pagbasa). Kaya sa malamig na cache ay ISANG
# pagbasa lang ang kasya sa `CYCLE_FEED_BUDGET_MS` at ang p90 na araw ay ~70 tick mula 04:00 ET,
# HINDI 9. Iyon ang dahilan kung bakit ang bagong sesyon ay NAGMAMANA ng ledger ng kapatid na
# sesyon ng parehong symbol-day (live_runner `_sibling_tape_cycle_state`) sa halip na basahin
# muli ang araw. Tatlong magkapatong na fence ang humahawak dito: ang bilang na ito, ang
# `CYCLE_FEED_BUDGET_MS` (predictive), at ang `max_reads` na ipinapasa ng runner.
CYCLE_FEED_READS_PER_TICK = 8

# ── ANG FENCE (refuter, 2026-09-11) ──────────────────────────────────────────
# INFRA na hangganan, hindi desisyon: walang tape na sinasagot ng mga numerong ito, at walang
# resultang nagbabago dahil sa kanila — pinipigilan lang nila ang isang sesyon na kainin ang
# buong runner batch. Hinango sa `chili_momentum_live_runner_batch_budget_seconds` (60 s ang
# ceiling ng BUONG batch; sinukat na batch wall p50 10.2 s / p90 14.8 s, max 648 s kung saan
# ISANG sesyon ang humarang ng 10.8 minuto at walang ibang sesyon — kasama ang HELD — ang
# nag-tick). Ang feed ay hindi kailanman ang dahilan niyon:
#   * STATEMENT_TIMEOUT_MS — kapareho ng idiom ng repo sa mainit na daan (paper_observer.py:30);
#     ang buhay na DB ay `statement_timeout = 0`, kaya walang fence kung hindi ito ilalagay.
#   * FEED_BUDGET_MS — kabuuang oras ng catch-up loop kada tick. ⚠️ MALI ANG DATING PREMISE
#     ("37x na headroom, kumakagat LAMANG kapag nagbago ang plano"): mainit na numero iyon
#     (4.9 ms). Sa MALAMIG na page ang isang pagbasa ay p50 1,231.7 ms / max 1,763.5 ([66]
#     review, tingnan ang `CYCLE_FEED_READS_PER_TICK`), kaya kumakagat ito sa BAWAT malamig na
#     catch-up. At dahil sinusuri lang ito BAGO ang bawat pagbasa, ang dating tick ay umaabot
#     sa budget + ISA pang pagbasa hanggang sa fence (~3.5 s sa loob ng FOR UPDATE na lock).
#     Ngayon ay PREDICTIVE: hindi nagsisimula ang pagbasa kapag ang lumipas + ang SINUKAT na
#     gastos ng huling pagbasa sa tick na ito ay lalampas sa budget (`budget` sa resibo ang
#     binding: elapsed_ms, last_read_ms, budget_ms). Ang UNANG pagbasa ng tick ay laging
#     pinapayagan (walang maihuhula, at kung wala ay hindi kailanman aabante ang ledger) — ang
#     STATEMENT_TIMEOUT ang humahawak dito.
CYCLE_FEED_STATEMENT_TIMEOUT_MS = 2000
CYCLE_FEED_BUDGET_MS = 1500

# SARGABLE NA CURSOR. Ang dating anyo — `observed_at > :last_at OR (observed_at = :last_at AND
# id > :last_id)` — ay walang MABABANG hangganan sa `observed_at`, kaya hindi ito naipapasok ng
# Postgres sa ix_iqfeed_trades_sym_at (symbol, observed_at DESC): nagiging Filter ito at ang
# backward index scan ay nagsisimula sa PINAKAMATANDANG hilera ng simbolo at itinatapon ang
# lahat ng nasa ilalim ng cursor. SINUKAT sa buhay na DB (WYHG 2026-09-08, as_of 20:00,
# LIMIT 5000, EXPLAIN (ANALYZE, BUFFERS), mainit ang cache):
#   cursor 14:00 (6-oras na puwang):  LUMA 272.9 ms / 61,208 buffer  ->  BAGO 4.9 ms / 2,053
#   cursor 19:00 (1-oras na puwang):  LUMA 136.9 ms /  4,764 buffer  ->  BAGO 15.3 ms / 4,730
# Ang `observed_at >= :last_at` ang nagbibigay ng saklaw sa index; ang row-comparison ang
# humahawak sa tabla sa loob ng parehong stamp. Mahalaga ito sa BAWAT equity session dahil ang
# cursor ay nagsisimula sa 04:00 ET at ang unang tick natin ay median 13:57Z ([59]).
_FEED_SQL = (
    "SELECT observed_at, id, price, size, bid, ask FROM iqfeed_trade_ticks "
    "WHERE symbol = :s AND price IS NOT NULL AND price > 0 "
    "AND observed_at >= :last_at AND observed_at <= :as_of "
    "AND (observed_at, id) > (:last_at, :last_id) "
    "ORDER BY observed_at ASC, id ASC LIMIT :n"
)

# ANG ACCESS PATH ([66], 2026-09-11). Ang sargable na cursor ay HINDI sapat. Ang tantya ng
# planner para sa (symbol, [cursor, as_of]) ay ang table-wide na dalas ng simbolo × ang
# selectivity ng saklaw ng oras — MAGKAHIWALAY na column, stats mula sa HULING ANALYZE. Para sa
# pangalang MABIGAT NGAYONG ARAW pero bihira sa buong table, ang tantya ay ilang daang hilera,
# MAS MABABA sa LIMIT; sa tingin ng planner ay babasahin din naman nito ang buong saklaw, kaya
# ang Bitmap Heap Scan + buong Sort ang mukhang mas mura kaysa sa naka-ayos na Index Scan — at
# binabasa nga nito ang BAWAT hilera ng saklaw bago mag-Sort, kaya walang silbi ang LIMIT.
# HINDI ito tungkol sa cold start o sa kinalalagyan ng cursor (ang dating komento rito ay MALI —
# review 2026-09-11): sa live, ang BDRX 22264 ay bumagsak sa cursor 14:43:22 matapos ang 80,000
# print, ang FTFT 22275/22288 sa 13:17:19 matapos ang 30,000; at ang autoanalyze ng 18:05:46Z ay
# nagbalik sa FTFT@13:17 sa Index Scan kahit naka-ON ang bitmap. SINUKAT (buhay na DB, 18:25Z,
# EXPLAIN, naka-ON ang bitmap, LIMIT 5000): BDRX@08:00 tantya 2,901 (tunay ~341k) ⇒ Bitmap;
# BDRX@14:43 705 ⇒ Bitmap; LBGJ/SXTC@08:00 196 ⇒ Bitmap; FTFT@08:00 8,492 / @13:17 6,641
# (>= LIMIT) ⇒ Index Scan. Sa 2 s na fence ang Bitmap ay `read_failed` sa PAREHONG pagbasa sa
# BAWAT tick (live 09-11: 9 sesyon / 6 simbolo — BDRX, BTCT, CRMT, FTFT, LBGJ, SXTC; ang 2 fill
# ng BDRX ay `no_tape_state`). Naka-off ang bitmap ANUMAN ang cursor: Index Scan Backward +
# Incremental Sort na humihinto sa LIMIT. GASTOS: 28.9 ms / 953 buffer ang unang sukat (BDRX@
# 08:00, `hit=952 read=1` — MAINIT ang cache noon, hindi ang karaniwang catch-up); MALAMIG p50
# 1,231.7 ms (tingnan ang `CYCLE_FEED_READS_PER_TICK`). Kaparehong idiom ng
# trade_tick_retention.py.
_FEED_ACCESS_PATH_GUC = "enable_bitmapscan"


def _db_error_name(exc: BaseException) -> str:
    """Ang KLASE ng pagkabigo (ang psycopg2 na `.orig` kapag SQLAlchemy ang bumalot)."""
    return type(getattr(exc, "orig", None) or exc).__name__


def _open_feed_fence(db: Any) -> tuple[Any, dict[str, Any] | None]:
    """Buksan ang fence ng feed sa SARILING SAVEPOINT (Postgres LAMANG). ``(savepoint, resibo)``.

    BAKIT SAVEPOINT (review 2026-09-11): ang `SET LOCAL` na tumakbo sa panlabas na transaksyon
    ng tick at TINANGGIHAN ng Postgres ay nag-a-abort sa BUONG transaksyon — ang feed read, ang
    reset, at ang natitirang tick ay `InFailedSqlTransaction`. Kaya:
      * ang fence ay nasa sariling savepoint na ROLLED BACK sa `_close_feed_fence` — ang
        GUC ay subtransaction-scoped, kaya WALANG tumatagas sa natitirang tick at walang
        `SET ... DEFAULT` na maaaring bumagsak (kaparehong idiom ng `bounded_fetchall`);
      * ang access-path pin ay nasa SARILI pang savepoint sa loob niyon: ang pagtanggi ay
        nagro-rollback LAMANG sa pin, kaya ang timeout fence ay nananatili, at ang pagtanggi ay
        NAKAPANGALAN sa resibo (`pin: refused`, `pin_error`) — hindi tahimik na fallback sa
        planong nagbulag sa ledger.
    ``(None, None)`` sa hindi-Postgres (ang sqlite na fake ng suite ay hindi kilala ang GUC)."""
    try:
        bind = db.get_bind()
        if str(getattr(getattr(bind, "dialect", None), "name", "")) != "postgresql":
            return None, None
    except Exception:
        return None, None
    begin_nested = getattr(db, "begin_nested", None)
    if not callable(begin_nested):
        return None, {"timeout_ms": None, "pin": None, "error": "no_savepoint"}
    from sqlalchemy import text as _sql

    try:
        sp = begin_nested()
    except Exception as exc:
        return None, {"timeout_ms": None, "pin": None, "error": _db_error_name(exc)}
    try:
        db.execute(_sql(f"SET LOCAL statement_timeout = '{int(CYCLE_FEED_STATEMENT_TIMEOUT_MS)}ms'"))
    except Exception as exc:
        _close_feed_fence(sp)
        return None, {"timeout_ms": None, "pin": None, "error": _db_error_name(exc)}
    receipt: dict[str, Any] = {"timeout_ms": int(CYCLE_FEED_STATEMENT_TIMEOUT_MS), "pin": None}
    try:
        with begin_nested():
            db.execute(_sql(f"SET LOCAL {_FEED_ACCESS_PATH_GUC} = off"))
        receipt["pin"] = f"{_FEED_ACCESS_PATH_GUC}=off"
    except Exception as exc:
        receipt["pin"] = "refused"
        receipt["pin_error"] = _db_error_name(exc)
        logger.warning(
            "[tape_cycles] access-path pin refused (%s): the feed runs on the planner's plan",
            receipt["pin_error"],
        )
    return sp, receipt


def _close_feed_fence(sp: Any) -> None:
    """ROLLBACK TO SAVEPOINT: ibinabalik ang BAWAT GUC ng fence sa dati. Ang feed ay SELECT
    lamang (walang isinusulat sa loob ng fence), kaya walang nawawala sa rollback."""
    if sp is None:
        return
    try:
        rollback = getattr(sp, "rollback", None)
        if callable(rollback):
            rollback()
        elif hasattr(sp, "__exit__"):
            sp.__exit__(None, None, None)
    except Exception:
        logger.debug("[tape_cycles] feed fence close failed", exc_info=True)


def feed_scanner_from_db(
    scanner: PullbackCycleScanner,
    symbol: str | None,
    *,
    db: Any,
    session_start: Any,
    max_prints: int,
    as_of: Any = None,
    max_reads: int | None = None,
) -> dict[str, Any]:
    """Isang BOUNDED na pagbasa kada tawag: ang susunod na ``max_prints`` na print pagkatapos
    ng cursor ng scanner, hanggang sa as-of.

    BAKIT PAUNTI-UNTI AT HINDI ISANG BUONG-ARAW NA BACKFILL: ang buong-araw na tape ay 272k–360k
    print (WYHG 09-08 = 272,499; RDHL 08-31 = 360,297) — isang blocking na backfill sa unang
    tick ay pipigilin ang buhay na lane nang ilang minuto. Ang scanner ay incremental, kaya ang
    catch-up ay nangyayari sa loob ng ilang tick SA MAINIT NA CACHE (sa malamig ay ~isang
    pagbasa kada tick — kaya minamana ng bagong sesyon ang ledger ng kapatid nito sa
    live_runner) at ang resibo ay nag-uulat ng ``caught_up``: ang desisyong ginawa habang
    naka-backfill pa ay HINDI nagpapanggap na kumpleto.

    ``max_reads`` ang bilang ng pagbasa na pinapayagan sa tawag na ito (default
    ``CYCLE_FEED_READS_PER_TICK``). Ipinapasa ito ng runner: ang catch-up ay para sa mga
    estadong maaari pang pumasok — ang ledger ay binabasa LAMANG ng entry sizing, kaya ang
    isang HELD na sesyon ay hindi nagbabayad ng DB na oras sa UNAHAN ng stop/trail/scale-out.

    Ibinabalik ang ``{fed, reads, caught_up, ms, to, last_read_ms, fence, budget_hit, budget}``.
    Fail-open: ang error ay ``reason: read_failed`` + ``error`` (klase) — at ang ``fed``/``to``/
    ``ms`` ay ang TOTOONG nakain na ng ledger bago ang bigong pagbasa (review 2026-09-11: ang
    dating resibo ay ``fed 0`` kahit 30,000 print na ang nakain at gumalaw na ang cursor).
    """
    s = (symbol or "").strip().upper()
    out: dict[str, Any] = {"fed": 0, "reads": 0, "caught_up": False}
    if not s or db is None or s.endswith("-USD"):
        out["reason"] = "no_equity_tape"
        return out
    try:
        n = max(1, int(max_prints))
    except (TypeError, ValueError):
        return out
    try:
        reads = max(0, int(max_reads if max_reads is not None else CYCLE_FEED_READS_PER_TICK))
    except (TypeError, ValueError):
        reads = CYCLE_FEED_READS_PER_TICK
    if reads <= 0:
        out["reason"] = "no_reads_allowed"
        return out
    _sp, _fence = _open_feed_fence(db)
    if _fence is not None:
        out["fence"] = _fence
    fed = 0
    _t0 = time.monotonic()
    _read_t0: float | None = None
    _last_read_ms: float | None = None
    try:
        from sqlalchemy import text as _sql

        from .optional_db_read import optional_fetchall

        ao = _asof_default(as_of)
        ao = ao.replace(tzinfo=None) if getattr(ao, "tzinfo", None) is not None else ao
        stmt = _sql(_FEED_SQL)
        for _ in range(reads):
            _elapsed = (time.monotonic() - _t0) * 1000.0
            if _elapsed >= CYCLE_FEED_BUDGET_MS or (
                _last_read_ms is not None and _elapsed + _last_read_ms > CYCLE_FEED_BUDGET_MS
            ):
                # INFRA budget, hindi desisyon: huminto tayo sa pagbasa, HINDI sa pagdedesisyon.
                # Ang `caught_up` ay nananatiling False kaya ang conditioning ay hindi kumakagat
                # sa isang ledger na nasa likod pa (tingnan ang gate sa live_runner).
                # PREDICTIVE: ang SINUKAT na gastos ng huling pagbasa ang hula sa susunod — hindi
                # na nagsisimula ang pagbasang lalampas sa budget (dating budget + 1 pagbasa).
                out["budget_hit"] = True
                out["budget"] = {
                    "elapsed_ms": round(_elapsed, 1),
                    "last_read_ms": (round(_last_read_ms, 1) if _last_read_ms is not None else None),
                    "budget_ms": int(CYCLE_FEED_BUDGET_MS),
                }
                break
            last_at = scanner.last_observed_at or session_start
            if isinstance(last_at, str):
                from datetime import datetime as _dt

                last_at = _dt.fromisoformat(last_at)
            last_at = (
                last_at.replace(tzinfo=None) if getattr(last_at, "tzinfo", None) is not None else last_at
            )
            _read_t0 = time.monotonic()
            rows = optional_fetchall(
                db,
                stmt,
                {"s": s, "as_of": ao, "last_at": last_at, "last_id": int(scanner.last_id or 0), "n": n},
            )
            _last_read_ms = (time.monotonic() - _read_t0) * 1000.0
            _read_t0 = None
            out["reads"] = int(out["reads"]) + 1
            fed += scanner.feed(rows)
            if len(rows) < n:  # naabutan na ang tape
                out["caught_up"] = True
                break
    except Exception as exc:
        logger.debug("[tape_cycles] feed_scanner_from_db read failed sym=%s", s, exc_info=True)
        out["reason"] = "read_failed"
        # Ang KLASE ng pagkabigo sa resibo ([66]): ang `QueryCanceled` (fence) ay ibang sanhi sa
        # nawalang koneksyon — "read_failed" lang dati, kaya 9 na sesyon ang bulag nang walang WHY.
        out["error"] = _db_error_name(exc)
        if _read_t0 is not None:
            # Ang bigong pagbasa ay PAGBASA rin, at kung gaano karami ng fence ang nasunog nito
            # ang binding na halaga (QueryCanceled sa ~2,000 ms = ang fence ang pumutol).
            out["reads"] = int(out["reads"]) + 1
            out["failed_read_ms"] = round((time.monotonic() - _read_t0) * 1000.0, 1)
    finally:
        _close_feed_fence(_sp)
    out["fed"] = int(fed)
    out["ms"] = round((time.monotonic() - _t0) * 1000.0, 1)
    out["to"] = scanner.last_observed_at
    if _last_read_ms is not None:
        out["last_read_ms"] = round(_last_read_ms, 1)
    return out
