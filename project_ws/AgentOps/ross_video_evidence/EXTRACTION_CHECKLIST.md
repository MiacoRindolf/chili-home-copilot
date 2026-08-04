# Ross Recap → `trades_*.json` Extraction Checklist

Procedure for turning a downloaded Ross Cameron recap video into a
`<videoId>/trades_YYYY-MM-DD.json` file (schema **`chili.ross_recap_trades.v1`**)
that `scripts/build_ross_manifest.py` (in the ross-parity worktree) can merge
into `manifest.json` (schema `chili.ross_ground_truth_manifest.v1`).

**Exemplar file:** `2UpK6vs0MVQ/trades_2026-07-16.json` — copy its shape.
**Ground rules:** transcript extraction is AFTER-FACT GRADING EVIDENCE ONLY
(see `usage_constraints` in the manifest — never event-time strategy input,
never executable-price evidence). No DB writes. Small vs main account is
preserved per row and **never summed across accounts**.

## 1. Inputs

- `<videoId>/transcript_ts.txt` — `[MM:SS.ss]`-stamped caption lines (primary).
  `transcript_flat.txt` is for grep only; always resolve claims back to a
  timestamped line.
- Video metadata (title, published date) → `video_title`, `published`.
- If frames were extracted (`frames/fNNNN.jpg`, NNNN = video second), note it,
  but transcript-only files must set `"frames_audited": false`. Frame auditing
  is a separate pass (AUDIT_REPORT batches) that supersedes transcript claims.

## 2. Establish the trading day (do NOT trust the upload date)

- Upload date ≠ session date (q-ywzhtkeY8: uploaded 07-22, "it's only Tuesday"
  → trading_day 07-21; miyJZq-5uIg: task premise said Jul-8, ToS panel stamp
  proved Jul-9). Use in-video cues: weekday mentions, "today's" catalysts,
  broker panel date stamps, challenge-day countback from a known anchor
  (2026-07-16 = Day 21). Record the reasoning in `trading_day_note` /
  `challenge_day_note` when inferred.

## 3. Symbol garble cross-checks (auto-captions ARE wrong)

Captions garble tickers constantly. Never emit a symbol from sound-alone.
Precedents: "Ruby"→RUBI, "VRX"→VRAX, "CF"/"CNF"→CANF, "GEM"/"JM"→JEM,
"LHI"/"LH AI"→LHAI, "S dot"→SDOT, "CX"→CETX, "PPC"→PPCB, "YD"→YRD,
"SVR-S-um-R"→SVRE, "VWave"→VWAV, "CZ/CLZ"→CELZ, "Q-U-N-C-Y"→QUCY,
"VTAC"→VTAK. Verification ladder (any ONE strong hit qualifies as verified):

1. **Catalyst/press-release match** — search the day's PR wires for the
   narrated catalyst (e.g. GlobeNewswire RUBI NAV release 2026-07-16 08:30 ET).
2. **Scanner/float/price context** — narrated float, price, %-gain must match
   a real ticker for that day.
3. **Chart-header / order-row frames** if available (authoritative).

If unresolved: keep the row, put the raw sound in `as_heard`, set
`"verified": false` (see S2sOq-stPgA "A J N" → `AGEN?`). Watchlist rows with
no recoverable symbol at all get `"symbol": null` + `caption_text` (the
builder skips them). Beware REAL near-twins (DXF vs DXST both existed on the
same scanner) — never "correct" a ticker without positive evidence.

## 4. One row per trade; one row per watchlist pass

- `trades[]`: one object per distinct trade (or per wave Ross narrates as a
  separate in/out). Fields: `symbol`, `company`, `account` (STRING containing
  "small" or "big/main" — required whenever the video covers both accounts),
  `side`, `entry_et` (`"~HH:MM-HH:MM"` ranges are fine), `entry_session`,
  `entry_trigger`, `entry_px` (+`entry_px_note`), `exits[]`
  ({portion, px_approx, et, note} — narrated levels stay `px_approx`; note
  when a level might be the move's high rather than his fill, e.g. VRAX
  "7.40"), `size` (+`size_note`), `pnl_claimed` (+`pnl_note`), `confidence`,
  `catalyst`, `move_shape`.
- `watchlist_no_trade[]`: one object per name Ross reviewed and passed on:
  `{symbol, reason, ...}` (+ `gap_pct_approx`, `conditional_trigger` when
  stated). Every "no setup"/"too cheap"/"too expensive"/"pass" verdict is a
  gradeable reject — capture them all.
- Day-level: `day_pnl_claimed` (+ which account!), `account_after`,
  `account_claims` (balance / return% / trade counts / accuracy).
- Ambiguous possibly-double-counted waves: keep the row, say so in `pnl_note`
  and grade confidence low (see 49ykxodJcFc trade 5).

## 5. Per-field confidence grading (drives manifest `pnl_confidence`)

Grade each field in a `confidence` object with prose that STARTS with the
grade word — the builder maps mechanically:

- `pnl` starting with `high` or containing `stated verbatim` →
  manifest `stated_verbatim` (an exact dollar figure spoken on tape:
  "$587.78"). Round narrations ("about $20,000") are **medium** → `narrated`.
- `medium (...)` → `narrated`. Running-P&L deltas, per-trade splits implied
  from a day total, "likely exactly…" reconstructions → say `derived` /
  `implied` / `likely` / `approx` in the note — any of these words demotes
  stated_verbatim to narrated in the builder.
- `low` / `n/a` → `inferred` (or null when `pnl_claimed` is null — leave
  `pnl_claimed` null rather than inventing a number; put derived estimates in
  `pnl_note` only).
- `frame_verified` is RESERVED for the frame-audit pass (broker-panel amounts
  in an AUDIT_REPORT). A transcript extraction can never claim it.

Same grading style for `symbol`, `entry_px`, `entry_time`, `exits` — cite the
basis in parentheses (stated / price-anchored / inferred sequence / tape
cross-check).

## 6. Small vs main account detection

- Challenge-day framing ("Day 21", ThinkOrSwim/Schwab panels, $2k-challenge
  balance arc ~$20-30k) → `small`.
- Lightspeed/`ROSCAM`, "retirement account", "big account", five-figure
  single-trade P&L → `main`.
- Both-account videos: set `account` on EVERY trade row. A big-account figure
  mentioned inside a small-account recap goes in
  `big_account_pnl_claimed_separate` on that trade (the builder emits a
  separate `main` row) — never summed.
- File-level default: `challenge_day` non-null or broker containing "small"
  defaults account-less trades to `small`; otherwise leave explicit.

## 7. Sanity checks before saving

- Per-trade P&L claims vs day total (e.g. NXTC +1,400 + UBXG +740.99 vs day
  ~2,140.99) — note derivations, don't force reconciliation
  (`usage_constraints`: day P/L need not equal the sum of trade P/L).
- Time claims vs CHILI tape when available (`tape_coverage` blocks are
  optional enrichment; ERNA precedent: caption "9:09" REFUTED by tape). Keep
  UTC/ET labels explicit. The manifest builder ignores tape blocks
  (`tape.live_covered` stays null — annotation is a separate, DB-touching
  step).
- Valid JSON, UTF-8, one file per video: `<videoId>/trades_<trading_day>.json`.
- Run `python scripts/build_ross_manifest.py --check` from the ross-parity
  worktree — it must report DRIFT (your new rows), then run the builder and
  re-run `--check` until OK. Duplicate `manifest_id`s abort the build.

## 8. What the builder does with your file (so you don't duplicate work)

- Every trade row → one manifest window (`expected_action: "trade"`).
- Every watchlist row → one `expected_action: "reject"` window.
- If a frame-audited curated row in `curated_windows.json` covers the same
  (symbol, date, account) 1:1, the curated row WINS and your row merges into
  it (refs appended; missing side/window/entry/catalyst filled; exit px never
  merged).
- `review_manifest.json` certifiability booleans can downgrade
  `pnl_confidence` afterwards — don't pre-downgrade for that reason.
