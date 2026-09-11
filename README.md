# IntuiCryp Prediction Fund

A four-agent **paper-trading fund** on Polymarket crypto markets.

| | |
|---|---|
| **Capital** | $100 (paper) |
| **Target** | $1,000 — 10x |
| **Burn** | $15/week ($5 VPS + $10 tokens) = $2.14/day |
| **Venue** | Polymarket (crypto markets) |
| **Mode** | Paper only. Hold to resolution. |
| **Status** | Live, cycling every 30 min. **0 positions booked** — no dislocation clears the cost hurdle |

> **There is no signing key anywhere in this repository.** It reads public,
> keyless APIs and keeps its own ledger. It cannot place an order by
> construction, not by configuration.

---

## Table of contents

- [The thesis, and why it is brutal](#the-thesis-and-why-it-is-brutal)
- [The four agents](#the-four-agents)
- [Data flow](#data-flow)
- [The economics](#the-economics)
- [The cost model](#the-cost-model)
- [Why the fund never exits early](#why-the-fund-never-exits-early)
- [The models](#the-models)
- [Vol anchoring: the correction that made this a fund](#vol-anchoring-the-correction-that-made-this-a-fund)
- [Sizing policy](#sizing-policy)
- [Risk and survival policy](#risk-and-survival-policy)
- [Settlement](#settlement)
- [How the audit works](#how-the-audit-works)
- [State and ledger design](#state-and-ledger-design)
- [The learning loop](#the-learning-loop)
- [Running it](#running-it)
- [Repository layout](#repository-layout)
- [Build log: findings that changed the design](#build-log-findings-that-changed-the-design)
- [Known limitations](#known-limitations)
- [Roadmap](#roadmap)

---

## The thesis, and why it is brutal

Prediction markets on crypto are the most *structurally* interesting place for a
small account to trade digital options: the payoff is binary and bounded, the
instruments are transparent, and the underlying (BTC/ETH spot and volatility) is
observable in real time. A "will BTC be above $78k tomorrow" share is a European
digital option priced at 0–$1, and it can be valued with the same mathematics as
any listed option.

The problem is not the pricing. It is the **cost**.

This fund exists inside a hard survival constraint: it burns $15/week against a
$100 book. That is **15% of capital per week** just to stand still. The equity
recurrence is

```
E(t+1) = E(t) * (1 + r) - 15
```

whose flat-line is `E = 15/r`. Below **~15%/week** the book does not merely
underperform — it compounds *downward* toward zero. Runway with zero edge is
**6.7 weeks**.

So the design question is never "what is a good trade?" It is "what does a trade
have to look like to be worth surviving for?" Everything below follows from that,
including the refusal to trade.

---

## The four agents

Each agent has one mandate and cannot do another's job. Agent 4 exists
specifically to disbelieve Agents 1–3.

| # | Agent | File | Mandate | Hard output |
|---|---|---|---|---|
| 1 | **SCOUT** | `scout.py` | Find the tradeable universe; reject everything else | candidates + a *recorded reason* for every rejection |
| 2 | **FORECASTER** | `forecaster.py` | Price the crypto reality | model probability + vol inputs |
| — | **smile** | `smile.py` | Re-anchor that probability to the market's own vol curve | `p_trade` — the number that may actually be traded |
| 3 | **RISK & EXECUTION** | `risk.py` | Size it, fill it against the real book, refuse what doesn't clear costs | position or refusal |
| 4 | **AUDITOR** | `auditor.py` | Assume the others are wrong and try to prove it | PASS/FAIL, exit code 1 on findings |

`cycle.py` orchestrates. `report.py` prints the economics. `notify.py` decides
whether a cycle deserves a message.

---

## Data flow

```
                    ┌─────────────────────── upstreams (read-only, keyless) ──┐
                    │  Gamma API  ·  CLOB API  ·  Binance public klines      │
                    └───────────────────────┬───────────────────────────────┘
                                            │
  1. SCOUT       discover → filter → attach live order book + fee schedule
                                            │
  2. FORECASTER  digital Φ(d2) / one-touch barrier  (realized vol)
                                            │
     smile       build the strike ladder → least-squares σ across it
                 → re-price → p_trade  (the edge is now a strike dislocation,
                                        not a volatility opinion)
                                            │
  3. RISK        simulate the fill by walking the real ask book
                 total cost = VWAP path + taker fee
                 gate: EV on stake ≥ 3% AFTER costs, else refuse
                 size: fixed fraction (Kelly computed and stored for contrast)
                                            │
                 ┌──────────────────────────┴──────────────────────────┐
                 │ snapshot raw book → state/snapshots/<pos>.json      │
                 │ book position    → state/ledger.jsonl (append-only) │
                 └──────────────────────────┬──────────────────────────┘
                                            │
  4. AUDITOR     recompute the fill from the raw snapshot, independently:
                 shares · vwap · fee · all-in · equity replay · hold invariant
                 → any disagreement is a finding, exit 1
```

---

## The economics

### The hurdle — burn as a share of the book

| Equity | $15/wk as % of book |
|---|---|
| $100 | **15.0%** |
| $150 | 10.0% |
| $250 | 6.0% |
| $500 | 3.0% |
| $750 | 2.0% |
| $1,000 | 1.5% |

Runway with zero edge: **6.7 weeks.**

### Required return — `E(t+1) = E(t)(1+r) − 15`, $100 → $1,000

| weekly r | flat-line equity | $100 sits… | weeks to $1,000 |
|---|---|---|---|
| 0% | ∞ | bleeds | never, dies |
| 5% | $300 | bleeds | never, dies |
| 10% | $150 | bleeds | never, dies |
| 15% | $100 | break-even | never grows |
| 18% | $83 | grows | 25 |
| 20% | $75 | grows | 20 |
| 25% | $60 | grows | 15 |
| **30%** | $50 | grows | **12** |
| 40% | $38 | grows | 9 |
| 50% | $30 | grows | 7 |

The drag is self-correcting: once the book clears ~$250 the burn is a 6%/week
problem instead of a 15% one. **$250 is the real first milestone, not $1,000.**

Reproduce with `python3 report.py`.

---

## The cost model

**The authoritative fee rate is read from each market's own payload**, not from
documentation or press coverage:

```json
"feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": true, "rebateRate": 0.2},
"feeType": "crypto_fees_v2"
```

```
taker_fee = rate * shares * p * (1 - p)          # rate = 0.07 for crypto
```

Crypto markets are uniformly 0.07; `tech_fees` and `finance_prices_fees` are
0.04. `takerOnly: true` means only takers pay; makers pay 0 and receive a 20%
rebate of collected taker fees. **Polymarket charges nothing on settlement.**

Measured on a $1 stake with a 1-tick spread:

| price | fee % of stake | round trip % of stake |
|---|---|---|
| $0.05 | 6.65% | **33.3%** |
| $0.10 | 6.30% | 22.6% |
| $0.20 | 5.60% | 16.2% |
| $0.30 | 4.90% | 13.1% |
| $0.50 | 3.50% | 9.0% |
| $0.70 | 2.10% | 5.6% |
| $0.90 | 0.70% | 2.5% |

Note the shape: the fee curve peaks at $0.50 (a coin flip is the most expensive
contract to trade), but the *spread* dominates at low prices, which is why cheap
longshots are the most expensive thing on the board to enter and exit.

---

## Why the fund never exits early

Round trips cost 2.5–33.3% of stake. Exiting early hands that to fees and
spread, and it is unrecoverable — the whole point of a binary is that the payoff
is fixed at $1.

So `ledger.py` has **no closing function at all**. There is no `close_position`,
no `exit_position`, no `sell`. `selftest.py` asserts that absence, and
`auditor.py` fails the run if any exit-shaped event ever appears in the ledger.
The fund buys once, pays the taker fee once, and holds to the oracle.

The cost of that choice is real and accepted: capital is locked until expiry,
and the only way out of a bad position is to be wrong all the way to settlement.

---

## The models

Both are driftless lognormal over horizon `T` — drift 0 is a deliberate,
conservative choice for short-horizon crypto. No momentum assumption is smuggled
in. `σ` is Binance realized volatility (1h bars, 168-bar window).

**Digital (European)** — `bitcoin-above-78k-on-september-12-2026`

```
d2 = ( ln(S/K) - σ²T/2 ) / ( σ√T )
P(above) = Φ(d2)
```

**One-touch barrier** — `reach-<K>` (up) and `dip-to-<K>` (down), via the
reflection principle, with the already-breached case returning 1.0:

```
P(touch) = 2 · Φ( -|ln(K/S)| / (σ√T) )
```

Anything outside these families returns `not_priceable` and is skipped. The fund
does not trade what it cannot value — which is why hourly "up or down" coin
flips and token-launch markets never appear in the book.

---

## Vol anchoring: the correction that made this a fund

**This is the most important engineering decision in the repository.**

The first working version priced each market with realized vol and compared to
the market. It found a "+0.05 probability edge" on `bitcoin-above-78k` and
looked profitable. It was wrong.

```
market ask                                    0.150
realized-vol model (σ = 31.1%)                0.253
implied vol of the market's own price         ~20%
```

The "edge" was entirely explained by **our trailing vol being higher than the
market's forward vol**. That is a *volatility opinion*, not a mispricing. A $100
account punting a vol view it cannot evidence gets destroyed slowly, and it will
look like bad luck the whole way down.

The fix is to stop using our own vol. `smile.py` builds a **strike ladder** from
every sibling in the same event (same underlying, same expiry) and fits a single
least-squares σ across it, then re-prices every strike against that consensus.

An edge now means exactly one thing: **this strike is mispriced relative to the
vol curve its own siblings imply.** That is a testable dislocation rather than
an opinion. Realized vol is still computed and reported as context, so the
disagreement stays visible in the audit trail instead of hidden.

### The ladder is U-shaped — and that killed the median

Restoring the full ladder revealed the market's real structure. Implied vol per
strike, BTC 2026-09-12, spot ≈ 77,377:

| Strike | Ask | Implied vol |
|---|---|---|
| 70,000 | 0.999 | 0.678 |
| 74,000 | 0.992 | 0.390 |
| **78,000** | **0.180** | **0.186** |
| 80,000 | 0.022 | 0.350 |
| 84,000 | 0.002 | 0.606 |

That is a **strongly U-shaped smile**: the market prices fatter tails than
lognormal, so the near-money strike implies far *lower* vol than the wings.

A **median** across those strikes returns **0.433**, which would price the
near-money strike at 0.33 against an ask of 0.18 — a manufactured 0.15 edge,
precisely the failure this module exists to prevent. So the consensus is a
least-squares fit instead: a strike priced 0.998 barely moves as σ changes, so
its squared error is nearly flat and it **down-weights itself**, while the
near-money strike dominates. That is the right weighting, because near-money
strikes are where the fund trades.

Both values are stored (`sigma_market_median` = the fit,
`sigma_market_median_raw` = the median) with `sigma_method` recorded in the
forecast, so the choice is auditable rather than implicit.

### Two traps that silently emptied the universe

1. **The dead-zone filter must not remove strikes from the ladder.** Rejecting
   near-certain strikes (ask ≥ 0.97) is right for *trading*, but doing it in the
   Scout removed the very strikes the consensus needs. BTC strikes are $2,000
   apart against ~1.6% daily vol, so only ~2 land inside a 0.02–0.97 band —
   leaving groups of 2, below the 4-strike minimum. Every daily market was
   discarded, and **the fund's highest-volume instruments were 100% excluded**
   while it traded monthly barriers instead. Dead-zone strikes now stay in the
   candidate set as curve *input* and are declined in `risk.py`.
2. **Groups under 4 strikes are skipped**, never guessed at.

Sanity signal that anchoring works: on the one trade that briefly cleared the
gate, `σ_market = 0.574` and `σ_realized = 0.541` sat close together — the model
had stopped arguing with the market about vol and started finding strike-level
mispricing instead.

---

## Sizing policy

Two modes exist, and the default is a deliberate deviation from theory:

| mode | behaviour | why |
|---|---|---|
| `kelly` | fractional Kelly `f* = (p − c)/(1 − c)` | theoretically correct for a *known* edge |
| **`fixed_fraction`** (default) | flat 10% of equity | the only way a $100 book deploys at all |

Kelly on the edges this book actually offers sizes positions at roughly
**$0.43** — below any sensible minimum. A Kelly-only fund would hold a
mathematically perfect book of *nothing* and never grow, which is a fine way to
be right and broke.

`fixed_fraction` deliberately **over-bets** relative to Kelly. The full-Kelly
number is still computed and stored on every decision row so the over-betting is
visible in the audit trail rather than hidden behind a config flag. The left tail
that this buys is bounded by the position cap (20%), the concurrency cap (3) and
the survival ladder below.

---

## Risk and survival policy

| Control | Value | Purpose |
|---|---|---|
| Position cap | 20% of equity, $20 absolute | no single oracle call can end the fund |
| Concurrency | 3 open positions | limits correlated crypto exposure |
| Min EV on stake | **3%, after fees and spread** | the cost hurdle, enforced twice (at probe size and again at final size, because slippage can kill an edge between the two) |
| Min book depth | $500 | refuse markets too thin to fill honestly |
| Spread cap | 0.03 | refuse wide books |
| Price dead zone | skip ask ≤ $0.02 or ≥ $0.97 | near-certainties have no room left to pay the fee |
| Defensive | equity < $50 | no new positions |
| Dead | equity < $25 | fund reported dead |

The "enforced twice" detail matters: a trade approved at probe size can be
rejected at final size after real slippage. Both checks use the *same* gate.

---

## Settlement

Settlement is read from the real oracle, never assumed:

```python
market = gamma.markets/{id}
if market["closed"] and outcomePrices[0] in {~0, ~1}:
    won = outcomePrices[0] > 0.5          # index 0 is our "Yes"
    payout = shares * 1.0 if won else 0.0  # free
```

A market that is closed but whose outcome prices are not yet decisive is left
open and re-checked next cycle. There is no guessing and no self-certification.

---

## How the audit works

Agent 4 never sees the traders' reasoning — only the raw evidence they stored.
It re-derives everything:

| Check | What it proves |
|---|---|
| **Fill fidelity** | re-walks the snapshot book and recomputes shares / vwap / fee / all-in |
| **Fee identity** | the fee equals `rate · shares · p · (1−p)` at the market's own rate |
| **Size caps** | no position exceeded the absolute cap when it opened |
| **Hold invariant** | no exit event ever appears in the ledger |
| **Equity replay** | cash derived by replaying the append-only ledger equals stored cash |

`selftest.py` proves the auditor is **not vacuous** by tampering with a stored
fill (inflating shares by 1.5x) and asserting the audit FAILS. A green check
that cannot go red is worth nothing.

---

## State and ledger design

`state/ledger.jsonl` is **append-only**: every event is a line of JSON with a
timestamp, and equity is always derived by replay — never by mutating a running
total. That is precisely what allows the Auditor to disagree with the traders.

`state/snapshots/<position>.json` stores the **raw order book** behind every
fill, so a fill can be re-derived from first principles months later. The
snapshot is the evidence; the ledger row is only a claim.

`sizing_mode`, the cost hurdle and the burn all live in `config.json` with the
reasoning inline.

Runtime state is **not** committed (`.gitignore`): it changes every cycle.

---

## Running it

```bash
python3 cycle.py --dry-run   # full pipeline, books nothing
python3 cycle.py             # live paper cycle (accrues burn, settles, books)
python3 report.py            # economics + live state
python3 table.py             # the full trade table (see below)
python3 selftest.py          # integrity harness (10 checks incl. tamper detection)
python3 auditor.py           # standalone audit, exit 1 on findings
```

**`table.py`** renders the whole book in one view: every trade (open and
settled) with its **prediction type** (`BTC daily above`, `ETH weekly dip`, …),
strike, entry, exit-or-live-mark, stake, fee, PnL $ and %, plus totals for
realized/unrealized/net P&L, fees paid, and the burn split into
$10 tokens + $5 VPS. It is wired to the Telegram bot via the `table` skill.

**`calibration.py`** is how we find out whether the model is any good without
risking money. The fund may take no trades for weeks, and "we found no edge" is
otherwise indistinguishable from "the model is broken". So every cycle records a
prediction for **every priced market — traded or not** — and grades it against
the real oracle outcome at settlement:

| metric | meaning |
|---|---|
| Brier score | mean squared error of the probabilities; 0.25 = always saying 0.50 |
| skill score | `1 − Brier/0.25`; positive means better than a coin flip |
| reliability | bucketed predicted vs realized frequency — exposes over/under-confidence, which is exactly what matters in the tails we trade |

~51 predictions are recorded per cycle (deduped to one per market per 12h), so
a week yields hundreds of graded outcomes. A Telegram ping fires once per
100-prediction threshold crossed.

### Agent skills

Both are versioned in `skills/` **and** installed on the box under
`~/.hermes/skills/trading/`, so the Telegram bot answers them:

| skill | what it does |
|---|---|
| **`calib`** | runs `calibration.py`, then explains the **meaning** and **implications**: the Brier/skill/reliability numbers, a blunt verdict (NOT ENOUGH EVIDENCE / MODEL FAILS / MARGINAL / MODEL HOLDS), and what each reliability gap implies for policy |
| **`table`** | runs `table.py` — every trade with its prediction type, fees, PnL and the burn split |

A predictor is only useful if you know when it is lying, so `calib` refuses to
characterise the model below 30 distinct graded markets and says so.

**Deployment:** the fund runs on a VPS at `/root/fund`. This repository is the
source; the box is the runtime.

```bash
scp *.py config.json README.md run_cycle.sh root@<vps>:/root/fund/
cp run_cycle.sh /root/.hermes/scripts/fund_cycle.sh   # cron requires this path
scp skills/*/SKILL.md root@<vps>:/root/.hermes/skills/trading/<skill>/  # per skill
systemctl --user restart hermes-gateway              # reload skills
```

**Cron:** job `fund-cycle`, every 30 minutes, `--no-agent`, script
`fund_cycle.sh`, delivering to Telegram. It is **silent unless something
happened** — a booking, a settlement, a survival-mode change or an audit
failure. Steady-state "scanned 1,600 markets, found no edge" must never generate
a message, or the signal drowns.

---

## Repository layout

```
costs.py        fee curve, fill simulation, Kelly, edge-after-costs   ← the core
venue.py        Gamma + CLOB + Binance clients (read-only, no signing)
scout.py        Agent 1 — discovery (3 paths) and universe filtering
forecaster.py   Agent 2 — digital and one-touch barrier models
smile.py        implied-vol consensus anchoring (see correction above)
risk.py         Agent 3 — sizing, cost gate, paper fills, survival ladder
auditor.py      Agent 4 — independent verification
ledger.py       append-only ledger, burn accrual, settlement (no exit path)
cycle.py        orchestration of the four agents
report.py       economics + live state
table.py        the full trade table: type, PnL, fees, burn split
calibration.py  grades every prediction against its real outcome (Brier/skill)
notify.py       decides whether a cycle deserves a message
selftest.py     integrity harness, incl. auditor tamper detection
run_cycle.sh    cron entrypoint (silent-unless-notable)
skills/         agent skills shipped with the fund (versioned, not just on the box)
  calib/        "is the model honest?" — Brier/skill/reliability + what to do
  table/        the full trade table
config.json     policy: capital, burn, limits, hurdles (reasoning inline)
```

---

## Build log: findings that changed the design

These are the things that were *not* obvious and that altered the architecture.

**1. The published fee rate was wrong.** Widely-quoted figures of 0.06 / 0.0625
for Polymarket crypto are stale or US-venue specific. The market payload says
`rate: 0.07`, identical to Kalshi's coefficient. This removed a claimed
venue-cost advantage for Polymarket; it remains the venue on liquidity and open
read APIs, not on fees.

**2. A realized-vol model invents fake edges on every market.** See
[Vol anchoring](#vol-anchoring-the-correction-that-made-this-a-fund). This is
the single highest-value fix in the repository.

**3. The high-volume markets are not in the crypto tag.** The `crypto` tag on
Gamma is dominated by token-launch and regulation markets. The daily
`bitcoin-above-on-<date>` events — ~11 strikes, ~$2M/day, the most tradeable
thing on the venue — **do not appear in it at all**. Filtering the tag alone
returned **zero** priceable candidates out of 600 scanned. Discovery now merges
three paths: explicit event slugs, public search, and the tag scan as a
catch-all, deduped by market id.

**4. A filter that protected the wallet silently emptied the universe.** The
dead-zone rule (don't trade near-certain strikes) was applied in the Scout, which
removed the sibling strikes `smile.py` needs for a vol consensus — and because
BTC strikes are $2,000 apart with ~1.6% daily vol, only ~2 per day fell inside
the tradeable band. Groups of 2 < the 4-strike minimum, so **every daily market
was discarded** and all 52 recorded predictions were long-dated monthly
barriers. The fund's highest-volume instruments (~$560k/day) were 100% excluded,
and nothing in the output said so. Fixed by separating "informs the curve" from
"may be traded". Daily markets anchored: **0 → 18**.

**5. Kelly and this mandate are incompatible.** A 1–2 point probability edge is
real, and Kelly correctly says to bet $0.43 on it. Reported honestly rather than
worked around by silently changing the sizing formula.

**6. A green audit proves nothing until it can go red.** Hence the tamper test.

**7. My own tooling lied twice while I was investigating #4.** A probe filtered
slugs on the literal substring `above-on-`, but real slugs are
`bitcoin-above-78k-on-...` — so it reported "zero daily markets discovered" when
66 were being found. Separately, a hand-check of the least-squares fit concluded
it was broken; the arithmetic was wrong, not the code. Both times the fix was to
measure the actual value rather than reason about it.

---

## The learning loop

### Do we have one? No.

The fund **measures** but does not **adapt**. There is a feedback instrument with
no closure:

| component | what it does | learns? |
|---|---|---|
| `calibration.py` | grades every prediction against the real outcome | **No** — nothing consumes the result |
| `smile.py` | fits σ across the event's strike ladder | **No** — refits from scratch each cycle, retains nothing |
| `forecaster.py` | digital + one-touch barrier pricing | **No** — no parameter is fitted from outcomes |
| `risk.py` | hurdle, sizing, caps | **No** — every threshold is a constant in `config.json` |
| `scout.py` | universe filters | **No** — constants |

Everything learnable is currently a hand-set constant. That is a gap, not a
design choice, and it is worth saying plainly: **the fund cannot get better at
trading on its own.**

### The predecessor had a loop, and it failed in a specific way

The retired spot stack *did* learn: `reflect.py` rewrote `pair.yaml` variables
every ~5 applied trades (`entry.threshold`, `stop_loss_pct`,
`atr_stop_multiplier`, …). Its failure modes are the default failure modes of any
naive loop, and they are the requirements for the next one:

- it applied **no-ops** — `entry.threshold 50 -> 50` — and reported them as
  progress;
- it was limited to **one whitelisted knob per cycle**, so it tuned *exits* while
  the actual defect was the *entry signal*;
- and critically, **it never measured whether its own changes helped, and never
  reverted one.** Every change was permanent.

That last point is the whole lesson. Adaptation without accountability is not
learning, it is motion — and it produces a system that sounds like it is
improving while it drifts.

### Where the feedback must come from: not P&L

At $100 scale trade P&L is a hopeless learning signal. Per-trade returns from a
binary are enormously dispersed, so resolving a real edge against the noise
needs an unattainable number of observations:

| true edge per trade | trades needed to prove it (95%) |
|---|---|
| **7.1%** | **2,017** |
| 17.9% | 340 |
| 42.9% | 64 |

At the ~1 trade/day the fund actually sees, the first row is **5.5 years**.

Calibration needs far less, because it never asks "did we make money" — only
"when we said 0.25, did it happen 0.25 of the time":

| mis-calibration to detect | graded predictions needed |
|---|---|
| 0.15 | 32 |
| 0.10 | 72 |
| **0.08** | **113** |
| 0.05 | 288 |

And those samples arrive fast: ~51 markets are priced per cycle, deduped to one
prediction per market per 12h, giving roughly **50–100 graded predictions per
day** once markets are resolving (~350–700/week).

So: **~113 samples arriving at ~100/day, versus ~2,017 samples arriving at
~1/day.** In wall-clock terms, days against years — about three orders of
magnitude. Any learning loop at this fund must be built on calibration.

### The plan

**Layer 0 — a frozen baseline (prerequisite).**
Version every parameter set and keep an untouched copy of the model. No change
counts as an improvement without an out-of-sample comparison against that frozen
original. Without it nothing is attributable, and the loop is unfalsifiable.

**Layer 1 — probability calibration (fast, high-N).**
Fit a monotone map `p_cal = f(p_raw)` on graded predictions (isotonic regression,
or a coarse bucketed/Platt correction) and trade `p_cal` in place of `p_trade`.
Guards: shrink toward the identity when a bucket's N is small; never extrapolate
outside the observed range; refit on a schedule (weekly) rather than continuously
so policy is stable and each change is a recorded event; revert if reliability
worsens after a refit.

This is the highest-value layer. It corrects exactly the failure mode that
matters — over-confidence in the 0.0–0.3 buckets, where every position lives —
and it raises the *effective* hurdle in precisely the buckets where the model is
lying.

**Layer 2 — model structure (medium, needs no capital).**
Fit quality is measurable immediately, without waiting for outcomes. Compare
competing structures on held-out fit residuals — e.g. the single-σ ladder fit
versus a two-parameter (level + curvature) smile, which the measured U-shape
suggests is warranted. Adopt only if residuals improve on a day the model was
*not* fitted on.

**Layer 3 — policy (slow, low-N, last).**
Only the hurdle and sizing should ever adapt, and only once trade N is large
enough that the P&L table above means something. Adapt from **calibrated** EV,
never raw; never loosen the hurdle on a win streak (that is fitting luck); move in
bounded steps and record the expected effect each time.

**Layer 4 — the market itself (data already collected, unexploited).**
Every prediction stores both `sigma_market` (the fit) and `sigma_realized`. The
gap between them is the variance risk premium — measurable now, with no capital
and no loop, and the most likely place a genuine non-directional edge exists in
this market.

### Governance rules

1. **One change per cycle**, written as a hypothesis: the change, the metric it
   should move, and by how much. No bundling.
2. **Every change is falsifiable and revertible.** Record the prediction, measure
   it later, revert if it does not materialise. The predecessor never reverted
   anything; that is the bug not to repeat.
3. **Bounds on every learned parameter**, with human-only knobs — venue, trading
   direction, capital, and the cost model itself — permanently outside the loop.
4. **A frozen baseline and an out-of-sample gate** on every adoption.
5. **A kill switch:** on calibration degradation, revert automatically to the last
   known-good parameter set.
6. **No LLM inside the loop.** Deterministic code only. An LLM reasoning about
   whether the fund is doing well cannot be audited the way a recomputed fill can
   be, and the auditor's independence is this system's main defence.

### What I would not build

- **Learning from P&L at this scale** — the power table is the reason.
- **LLM-written strategy rewrites** — unauditable, and at 1–2 trades logged it
  would be fitting noise.
- **Touching the cost hurdle before calibration is trustworthy** — calibration and
  the cost hurdle are independent questions, and conflating them would let a
  modelling fix masquerade as a policy improvement.

### Build order

```
1. freeze baseline + version parameter sets      prerequisite; no learning yet
2. layer 1  probability calibration              highest power, days to validate
3. layer 2  smile structure (level + curvature)  no capital, immediate feedback
4. layer 4  variance risk premium                data already being collected
5. layer 3  policy adaptation                    only at sufficient trade N
```

Explicitly: **step 1 is not worth starting until calibration holds ~30+ graded
distinct markets**, because below that a loop would be fitting noise — which is
precisely the failure the predecessor shipped.

---

## Known limitations

Stated rather than buried:

- **Realized vol is backward-looking.** The σ band (±20%) is a crude error
  proxy, not a confidence interval.
- **Driftless GBM assumes no jumps and no fat tails.** Crypto has both; the
  barrier model will understate true touch probabilities around news events.
- **Market-implied vol is inverted from the ask**, so it carries the spread.
- **Settlement depends on the API flipping `outcomePrices` decisively.** A
  disputed UMA resolution would leave a position open past its end date.
- **Single venue.** Kalshi was evaluated (deeper on short-dated BTC brackets,
  1c ticks) but not wired in; it is the natural second price reference.
- **Taker-only.** Resting limit orders would earn the 20% maker rebate but risk
  non-fill; modelling that honestly is real work and is not done.

---

## Roadmap

1. Kalshi as a second reference price — a genuine cross-venue dislocation signal.
2. Maker-order modelling, to earn the rebate rather than pay the taker fee.
3. Per-event vol-smile **skew** modelling — the ladder fit is a single σ, but the
   measured smile is U-shaped, so a two-parameter (level + curvature) fit would
   describe it better than one number.
4. Act on calibration once the sample is large enough — the recorder is running
   (`calibration.py`), but the reliability table is only actionable past ~30
   graded markets.

---

*Paper trading only. No order has ever been, or can be, placed by this code.*
