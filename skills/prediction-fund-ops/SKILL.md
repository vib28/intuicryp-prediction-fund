---
name: prediction-fund-ops
description: "Use when operating the Polymarket paper fund on the VPS."
version: 1.0.0
author: Hermes
license: MIT
platforms: [linux, macos, windows]
---

# Prediction Fund Ops (IntuiCryp Prediction Fund)

A 4-agent paper fund trading **Polymarket crypto** markets. $100 capital, $1,000
target, $15/week burn, hold-to-resolution only. Lives at `/root/fund` on
`root@167.179.102.166`; local source of truth is
`OneDrive/Documents/Projects/Hermes/Prediction Fund`.

Agents: `scout.py` (universe) → `forecaster.py` (pricing) → `smile.py`
(vol anchoring) → `risk.py` (sizing + fills) → `auditor.py` (independent
verification). `cycle.py` orchestrates; `report.py` prints economics;
`notify.py` decides whether a cycle is worth a message.

## The fee rate is 0.07, and you must read it per-market

Do NOT trust press figures (0.06 / 0.0625 circulate). The authoritative rate is
in the Gamma market payload:

```
feeSchedule = {'exponent': 1, 'rate': 0.07, 'takerOnly': True, 'rebateRate': 0.2}
feeType     = 'crypto_fees_v2'
```

`fee = rate * shares * p * (1-p)`. Crypto markets are uniformly 0.07; tech and
finance_price markets are 0.04. Always read `feeSchedule.rate` per market.
Polymarket charges **nothing on settlement**, so the fund pays the taker fee
exactly once.

Measured round trip on a $1 stake with a 1c spread: 33.3% at $0.05, 9.0% at
$0.50, 2.5% at $0.90. **This is why `ledger.py` has NO exit function** — trading
in and out is mathematically dead. Never add one.

## PITFALL: a realized-vol model invents fake edges on every market

First live run claimed a "+0.05 probability edge" on `bitcoin-above-78k`. Cause:
realized vol 31% vs market-implied ~20%. That was a **vol opinion, not a
mispricing** — the failure mode that destroys naive quant books.

Fix (`smile.py`): fit ONE sigma that best explains the whole strike ladder
(least squares over log-sigma) and re-price every strike against it. An edge then
means only "this strike is mispriced relative to the curve its siblings imply".

Use the **fit, NOT the median of per-strike implied vols**. The ladder is
strongly U-shaped (the market prices fatter tails than lognormal), so a median is
dominated by the saturated wings: on BTC 2026-09-12 the median returned 0.433,
which prices the near-money strike at 0.33 against an ask of 0.18 — a
manufactured 0.15 edge, exactly the failure this module exists to prevent. A
least-squares fit weights by sensitivity instead: a strike priced 0.998 barely
moves as sigma changes, so it down-weights itself, while the near-money strike
dominates. The median is retained as `sigma_market_raw_median`, used only as the
fallback when the fit fails.

The consensus sigma is stored as `sigma_market` (renamed from
`sigma_market_median`, which named a method the code no longer used). Read it via
`smile.sigma_market_of(forecast)`, which accepts the old key too, so predictions
recorded before the rename remain usable evidence.

## PITFALL: the high-volume price markets are NOT in the crypto tag listing

Tag 21 (`crypto`) is dominated by token-launch/regulation markets. The daily
`bitcoin-above-on-<date>` events (11 strikes, ~$2M/day) are absent from it —
filtering the tag alone yields **zero** priceable candidates. Discover via THREE
merged paths: (a) `GET /events/slug/<asset>-above-on-<month>-<d>-<year>`,
(b) `GET /public-search?q=what price will bitcoin hit`,
(c) the tag keyset scan as catch-all. Dedupe by market id.

Parseable families and their models (driftless lognormal, sigma from Binance 1h
realized vol, 168 bars):
- `above-<N>k` → European digital, `Phi(d2)`
- `reach-<N>k` → one-touch up, `2*Phi(-|ln(K/S)|/(sigma*sqrt(T)))`
- `dip-to-<N>k` → one-touch down, same with the sign convention per direction

Gamma keyset returns `markets` (NOT `data`); CLOB `bids` ascend and `asks`
descend, so sort explicitly — never take `[0]` as best.

## Sizing: fixed_fraction, and say so out loud

Kelly on the edges this book actually offers sizes positions at ~$0.43 — below
any minimum, so a Kelly-only fund holds a mathematically perfect book of
nothing and never grows. `risk.sizing_mode` is therefore `fixed_fraction`
(10% of equity), which deliberately OVER-bets vs Kelly. Kelly is still computed
and stored on every row so the over-betting is visible in the audit trail
instead of hidden behind a config flag. Note the consequence honestly: $100 with
low-single-digit probability edges will mostly decline to trade.

## PITFALL: four classes of silent failure this codebase has actually shipped

Each of these ran without error and passed the audit while doing the wrong thing.
Check for all four when touching policy or verification code.

1. **A config key that no code reads.** `risk.py` carried its own
   `MIN_EV_ON_STAKE_PCT = 3.0` and never read `risk.min_ev_on_stake_pct`, so
   editing the hurdle in `config.json` changed nothing. `universe.min_liquidity_usd`
   was enforced nowhere either. Policy resolves through `config.py` only, and
   `config.validate()` fails loudly on a missing required key.
2. **A check that is computed and never applied.** The auditor assigned `max_pct`
   and then never used it, so the percent-of-equity cap was decorative for the
   fund's whole life — a fill could be 90% of the book and the audit would pass.
   If you compute a bound, apply it, and prove it can fire.
3. **A constant duplicated in three places.** The weekly burn lived in
   `config.json`, `ledger.py` and `table.py`; a change would have been silently
   half-applied. Derive it (`config.burn_split()`), never re-declare it.
4. **A name that lies about the method.** `sigma_market_median` held a
   least-squares fit, so every stored record misdescribed itself. Rename when the
   method changes, and keep a tolerant reader for already-recorded data.

Two more that cost real money or trust:

- **Cap arithmetic must use the same basis as the cap.** A top-up reserved room
  under the $20 cap using the GROSS stake while the cap applies to ALL-IN cost,
  so it overshot **by exactly its own fee** — that is how the live book came to
  hold a $21.01 position against a $20 limit. Discount room by the fee fraction
  and re-check the cap on the final fill numbers.
- **A wrong ledger kind silently breaks the replay.** A repeat order emits `add`,
  not `open`; the equity replay only handled `open`, so the first top-up ever
  made would have produced a phantom audit FAIL every cycle forever. Every event
  kind that moves cash must appear in the replay.

Also: a verification step that WRITES is not verification. `cycle.py --dry-run`
used to persist snapshots, `last_cycle.json` and predictions. It now computes
everything and writes nothing.

## Verify with a selftest, including a tamper test

`selftest.py` isolates ledger state into a temp dir (re-point the module
globals `ledger.STATE_DIR/LEDGER/ACCOUNT/POSITIONS/SNAPSHOTS`), injects a
synthetic dislocation, and runs 17 assertions: booked, shares==stake/cost, fee
identity, auditor PASS, **auditor FAILS a tampered snapshot**, auditor PASSES
once restored, a repeat order MERGES into one position with two fills, the equity
replay survives the `add` event, **the percent cap FIRES when the equity base is
shrunk**, the cap holds on an honest book, settlement pays $1/share, equity
replays, and no exit function exists. Every check is made to fail on purpose at
least once — a green audit that cannot go red is worth nothing.

## Burn is a rate — but bill one-off spend explicitly, never fold it in

`burn.total_weekly_usd` accrues per second. That is right for the recurring VPS +
tokens baseline and wrong for anything invoice-shaped: a long agent/dev session is
a one-off, and folding it into the rate would misdate it, dilute it across the
week, and hide it.

```bash
cd /root/fund && python3 ledger.py --one-off-burn 3.00 tokens "reason"
```

Keep it `kind: "burn"` so the Auditor's equity replay still reconciles — a
separate event kind would break the replay, which is exactly the class of bug the
`add`-vs-`open` defect came from. Burn attribution lives in
`account.burn_by_category`, NOT re-derived from the baseline 2:1 ratio: the ratio
would smear a tokens-only bill across the VPS share and overstate infrastructure
cost. `table.py` prints an `ADJUSTED` line per billed event; `calibration.py`
carries the same cost line. When the user reports an agent/dev token cost, record
it here before generating a table or calib, so the reported burn is the true one.

## Operations

```bash
python3 cycle.py --dry-run    # full pipeline, writes nothing
python3 cycle.py              # live paper cycle
python3 report.py             # economics + live state + calibration
python3 selftest.py           # 17-check integrity harness
python3 auditor.py            # standalone check, exit 1 on findings
python3 probe.py              # one pass, timed, with upstream call counts
python3 ledger.py             # account + totals, burn split by category
python3 ledger.py --one-off-burn 3.00 tokens "reason"   # billed, not accrued
```

**Measure a performance claim, never assert it.** `probe.py` times each stage and
counts upstream calls by host, and runs unchanged in an extracted old tree, so a
change can be A/B'd. It is how "memoize the vol lookups" became a verified
29.6s -> 3.7s (Binance calls 168 -> 7, total upstream 278 -> 117) instead of a
guess. Two structural wins are in place: `venue._cached` memoizes spot and
realized-vol per process (which for a cron cycle is exactly one pass), and
scanning is two-phase — all structural filters first with no network at all, then
the survivors' order books fetched concurrently (`scan.book_fetch_workers`,
default 8). The ~95 book fetches are irreducible (one per token) and now dominate
the cycle, so they are the remaining lever.

Cron `fund-cycle` every 30m, `--no-agent`, script `fund_cycle.sh` (copy of
`run_cycle.sh`, must live under `~/.hermes/scripts/`), delivers to the Telegram
DM. Silent unless a booking, settlement, survival-mode change or audit failure
occurs — steady-state "no edge found" must never generate a message.

`state/ledger.jsonl` is append-only; equity is derived by replay so the Auditor
can disagree with the traders. `state/snapshots/*.json` store the raw book
behind each fill so fills stay re-derivable. Cap `rejections.jsonl`
(`scan.rejections_max_bytes`) — ~1,500 rejections per cycle otherwise grows
without bound and the VPS disk is the binding constraint.
