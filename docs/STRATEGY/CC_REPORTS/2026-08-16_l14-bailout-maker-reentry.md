# 2026-08-16 — L14: post-bailout maker re-entry (A/B verdict: NET +232.74)

## TL;DR

Ang L2a churn class (~−450 ng canon-v4 −492 net) ay sunud-sunod na LOSING
bailouts na may 0–16s re-entry gaps na tumatawid sa ask bawat attempt. Ang
time-spacing ay **tinanggihan ng datos bago pa mabuo** (101-cycle sukat:
panalo median gap 4s, 71% <20s — mas mabilis pa sa mga talo), kaya ang lever
ay nagpapamura ng bawat attempt sa halip na magbawas ng attempts: ang equity
re-attempt sa loob ng 90s mula sa LOSING bailout ay nag-po-POST sa BID
(maker/join, walang repeg) sa halip na tumawid sa guarded ask.

## A/B (L14 vs canon v4, intended arm, parehong seeds)

| Window | v4 | L14 | Δ | |
|---|---|---|---|---|
| TVRD\|07-07 | −98.88 | **+64.02** | **+162.90** | 31e→7e — flagship flip |
| VTAK\|07-08 | −105.09 | **+14.37** | **+119.46** | 18e→5e |
| TC\|07-01 | +6.72 | +26.51 | +19.79 | mas murang re-entries |
| LHAI\|07-01 | −3.40 | −0.43 | +2.97 | |
| CLRO\|07-02 | −11.37 | −9.91 | +1.46 | |
| JLHL\|07-09 | +1.28 | +1.28 | **±0.00** | winner EKSAKTONG buo |
| CWD\|07-02 | −197.55 | −271.39 | **−73.84** | ⚠️ adverse selection |
| CELZ\|06-30 | +31.69 | timeout ×2 (18000s, 28800s) | — | runtime pathology |

**NET +232.74** sa 7 ok windows.

## Ang CWD trade-off (tinanggap nang may dokumentasyon)

Sa araw na PURO baba (CWD: zero panalo kahit kailan), ang bid-post ay
nafi-fill lang kapag bumabagsak ang presyo papunta sa iyo (adverse
selection). Naka-bound ito ng L13 symbol-day lockout (ang CWD-class ay
humihinto sa ~−1.5R×cap). **Follow-up lever (hindi blocker): fade guard** —
huwag mag-maker kapag kumukupas ang tape (nangangailangan ng sariling
discriminator study).

## CELZ runtime pathology

Sa ilalim ng L14, ang CELZ replay ay lumampas sa 3h/5h/8h caps (ang resting
maker limits ay dumadaan sa volume-capped fill model bawat grid step sa 981k-
row NBBO window — ang mabigat na simulation cost, hindi estratehiya). Winner-
safety evidence sa halip: TC +19.79 (bumuti ang winner) at JLHL ±0.00
(eksaktong hindi nagalaw). Kapareho ng UPC exclusion precedent.

## Mekanika

- `risk_policy.bailout_maker_reentry_decision` — pure; enabled + bailout-class
  + rb<0 + ≤90s window (replay-aware clock); fail-toward-legacy
- `live_runner`: `le["last_exit_at_utc"]` stamp + entry-seam branch sa tabi ng
  crypto maker-only precedent; emit `live_entry_bailout_maker_reentry`
- Config: `chili_momentum_bailout_maker_reentry_enabled` (True, live+ON) +
  `..._window_seconds` (90, ang ISANG base)
- Tests: 11/11 (`test_bailout_maker_reentry.py`)

## Kumulatibong linggo (canon lens)

−1,416.20 (v3, may lason) → −492.26 (v4, malinis) → **~−260 pataas na may
L14** — ang natitirang pula ay CWD-class na sakop ng L13 at ang churn residual
na target ng fade-guard/meta-label follow-ups.
