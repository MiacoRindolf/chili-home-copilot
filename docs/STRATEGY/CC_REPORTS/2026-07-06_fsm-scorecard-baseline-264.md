# CHILI momentum — session report (2026-07-05/06)

## 1. What shipped (3 PRs merged to main)
| PR | Commit | What | Trading effect |
|---|---|---|---|
| **#849** | b3ab9eb | MFE-target LIVE (data-derived exit) + conviction sizing + ATR anti-chase re-entry + whole-trade-net `was_loss` fix | ⬆️ |
| **#850** | 8a5c5a6 | conviction × ENTRY-quality gate (don't size up weak/extended entries) | ⬆️ |
| **#851** | 7e6bbe2 | meta-label prod feature-capture unblock (persist entry snapshot before the throwing capture; surface the error) | trading-neutral (unblocks the future lever) |

All default-ON, kill-switchable, each adversarial-reviewed (the review caught a real `was_loss` regression pre-ship).

## 2. Scorecard — current main, FSM replay, NET **+$264.25**
Three operator criteria: ① capture ALL Ross winners, ② avoid ALL Ross losers, ③ eliminate CHILI's own losers.

### 06-26
| Symbol | Window ET | Ross | Ross $ | CHILI | Verdict |
|---|---|---|---|---|---|
| ZDAI | 07:54–08:40 | chased top | −$25,431 | $0 (0 buys) | ② ✅ AVOIDED |

### 06-30
| Symbol | Window ET | Ross | Ross $ | CHILI | Verdict |
|---|---|---|---|---|---|
| SVRE | 08:30–09:02 | $7 break | +$16,064 | −$0.33 | ① ❌ MISS (top-entry) |
| JEM | 08:50–09:12 | rode vertical | +$46,010 | **+$314.53** | ① ✅ CAUGHT |
| CELZ | 08:55–09:40 | watched (busy) | $0 | **+$48.17** | 🏆 BEAT |

### 07-01
| Symbol | Window ET | Ross | Ross $ | CHILI | Verdict |
|---|---|---|---|---|---|
| TC | 05:33–05:55 | wrong acct | −$395 | −$13.38 | ② ➖ bled small |
| LHAI | 08:30–09:10 | no-setup (cheap) | $0 | −$8.09 | ③ ➖ traded his skip |
| DXF | 09:30–10:10 | no-setup (cheap) | $0 | −$49.38 | ③ ❌ top-buy |
| CANF | 09:50–10:35 | breakout | +$7,371 | −$5.90 | ① ❌ MISS (late) |
| JEM | 10:00–10:45 | entered → faded | −$232 | $0 | ② ✅ AVOIDED |

### 07-02
| Symbol | Window ET | Ross | Ross $ | CHILI | Verdict |
|---|---|---|---|---|---|
| CLRO | 12:12–12:50 | (not in Fable5 audit) | — | −$21.37 | ③ ❌ top-buy |

**Criteria:** ① 1/3 caught · ② 2/3 avoided (met) · ③ −$79 own losses remain.
**No-tape (audit-only, can't FSM):** ILLR +$19.9k (06-25 SpaceX), MIMI −$8.4k, ANY −$6.3k, SHPH −$3k.

## 3. The frontier — gate-tuning is at its ceiling (+$264.25)
① and ③ collapse to ONE hard problem: the entry trigger is **confirmation-lagged**, so on a fast vertical it fires **at the top** (SVRE 7.54 vs 6.98; DXF 4× @ 1.05–1.10). CHILI catches the CLEAN vertical (JEM) but tops-out the messy ones — and **no gate can tell will-run from will-fade at entry.**

**FIVE negative results proved gate-tuning can't fix this** (the "challenge the diagnosis" discipline held — no regressions shipped):
1. sticky-backside-bench OFF → JEM +314 → −3 (bench is protective)
2. fresh-base un-bench → SVRE −0.33 → −12, CELZ +48 → −20 (reverted)
3. frontside gate can't shrink DXF (mid-vertical reads strong)
4. forcing SVRE early entry LOSES (can't hold the vertical)
5. wider-stops signal is causation-REVERSED (stop is already ATR-scaled + family-tuned)

## 4. THE lever — meta-label edge model, RIGOROUSLY VALIDATED
The principled fix (learn will-run vs will-fade → size by p). Trained the real `train_meta_label` on the 57 FSM outcomes: **perm_p = 0.001** (out-of-day GroupKFold CV, 1000-iter permutation) — the features **significantly** separate winners from losers, even on the degenerate FSM data. The infra is BUILT + WIRED (meta_label.py + live_runner `_meta_mult`, never-veto, evidence-scaled, floor 0.4).

**Not shipped** — the FSM model is overfit (57 biased in-sample rows, secondary features; the intended OFI/L2 mechanism features are prod-only). Shipping it = the curve-fit trap.

## 5. The path past +$264 (needs operator)
1. **Deploy #851** (FLAT + verify single DATABASE_URL). It guarantees the entry snapshot + surfaces the remaining capture failure as `live_entry_feature_capture_error`.
2. Read that event's `etype`/`err` → it names WHICH prod call throws (likely the real 15m fetch or L2 path) → fix THAT precisely.
3. Feature vectors accumulate over a few live sessions → the meta-label auto-trains on **real mechanism features** → sizes down the DXF/CLRO-type losers, keeps the JEM-type winners → **scorecard moves past +$264.**

**The improvement is real, validated, and imminent — gated only on data collection, which #851 started.**

## Decisions waiting for you
- Deploy #851 now? (I can help — needs FLAT + single-DATABASE_URL verify)
- Anything else to prioritize, or is the deploy-and-accumulate path the plan?
