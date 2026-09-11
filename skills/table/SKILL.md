---
name: table
description: "Use when the user asks for the table / trade table / P&L table."
version: 2.0.0
author: Hermes
license: MIT
platforms: [linux, macos, windows]
---

# table — the Prediction Fund trade table

The old crypto-spot stack (`/app`, `/app-router`) is **RETIRED and archived**
(see `/root/archive/strategy-2026-09-11/`) — its containers are stopped and it
no longer writes state. All skill references to `/app/state/...` in the other
`trading/` skills are historical and must not be used for reporting.

The live book is the **IntuiCryp Prediction Fund** at `/root/fund`.

## Procedure

Run exactly this, and present its output:

```bash
python3 /root/fund/table.py
```

`table.py` renders the whole thing in one view; do NOT reconstruct it by hand or
read the raw JSON, and do not recompute the P&L. It prints:

1. **Header** — capital, starting equity, buy-in net, and the burn split
   ($15/wk = $10 tokens + $5 VPS) with what has been charged so far.
2. **Every trade** (open and settled): `#`, opened, **prediction type**, market
   slug, strike, entry, exit-or-mark, stake, fee, PnL $, PnL %, status.
3. **Totals** — realized, unrealized, net, fees paid, burn charged, and the
   figure net of everything.

## Prediction type column

Formatted `SYMBOL HORIZON KIND`, e.g.:

| label | meaning |
|---|---|
| `BTC 15min above` | 15-minute BTC market |
| `BTC 1h above` | hourly |
| `BTC daily above` | resolves within ~1.5 days |
| `BTC weekly dip` | weekly one-touch barrier downwards |
| `ETH monthly reach` | longer-dated upward barrier |

`KIND` comes from the model family: `above` = European digital, `reach` =
one-touch up, `dip` = one-touch down.

## Rules

- **Show every trade.** Never truncate the list, never show only recent rows.
- **Always include the fee and burn lines.** A P&L that ignores the $15/week
  burn is fiction — the burn is the reason the fund is in survival mode.
- Open positions are marked to the live book midpoint; `n/a` means the
  midpoint could not be fetched and the row falls back to cost basis (no profit
  is claimed on a guess).
- If there are no trades, say so plainly — "none yet, no dislocation has cleared
  the cost hurdle" is the correct and expected answer, not a failure.
- Keep the raw table in a code block so the columns stay aligned.

## Related

- Fund operations, models, cost model and known limitations:
  skill `prediction-fund-ops`.
- Economics only (burn hurdle, required return, cost curve): `python3
  /root/fund/report.py`.
