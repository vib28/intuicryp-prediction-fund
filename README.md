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
     smile       invert each sibling strike's implied vol → median consensus
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

The fix is to stop using our own vol. Within one event there are ~11 strikes on
the same underlying and expiry. `smile.py`:

1. inverts each strike's ask to its **implied vol** (bisection),
2. takes the **median** across siblings as the market's consensus vol for that
   expiry,
3. re-prices *every* strike against that consensus.

An edge now means exactly one thing: **this strike is mispriced relative to the
vol curve its own siblings imply.** That is a testable dislocation rather than
an opinion. The realized vol is still computed and reported as context, so the
disagreement stays visible in the audit trail instead of hidden.

The sanity signal that it works: on the one trade that briefly cleared the gate,
`σ_market = 0.574` and `σ_realized = 0.541` sat close together — the model had
stopped arguing with the market about vol and started finding strike-level
mispricing instead. Events with fewer than 4 strikes from which to establish a
consensus are skipped entirely rather than guessed at.

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

**Deployment:** the fund runs on a VPS at `/root/fund`. This repository is the
source; the box is the runtime.

```bash
scp *.py config.json run_cycle.sh root@<vps>:/root/fund/
cp run_cycle.sh /root/.hermes/scripts/fund_cycle.sh   # cron requires this path
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
notify.py       decides whether a cycle deserves a message
selftest.py     integrity harness, incl. auditor tamper detection
run_cycle.sh    cron entrypoint (silent-unless-notable)
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

**4. Kelly and this mandate are incompatible.** A 1–2 point probability edge is
real, and Kelly correctly says to bet $0.43 on it. Reported honestly rather than
worked around by silently changing the sizing formula.

**5. A green audit proves nothing until it can go red.** Hence the tamper test.

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
3. Per-event vol-smile fitting (currently a median) to detect skew, not just
   level.
4. Calibration tracking: record `p_trade` against realized outcomes, so the
   model's own bias becomes measurable over time.

---

*Paper trading only. No order has ever been, or can be, placed by this code.*
