"""
Report — the economics, always with transaction costs priced in.

Run:  python3 report.py

Includes the calibration section, because "are we making money" and "is the
model honest" are different questions and this fund can answer the second one
long before the first.
"""

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import calibration    # noqa: E402
import config         # noqa: E402
import costs          # noqa: E402
import ledger         # noqa: E402

# Equity levels the hurdle table is reported at — presentation only.
HURDLE_LEVELS = (100, 150, 250, 500, 750, 1000)
RETURN_SCENARIOS = (0.0, 0.05, 0.10, 0.15, 0.18, 0.20, 0.25, 0.30, 0.40, 0.50)
# Prices the cost curve is tabulated at — every position this fund holds lives
# in the lower half of this range.
COST_CURVE_PRICES = (0.05, 0.10, 0.20, 0.30, 0.50, 0.70, 0.90)


def economics() -> None:
    cfg = config.load()
    f, burn = cfg["fund"], cfg["burn"]
    cap, target = config.capital(cfg), config.target(cfg)
    wk = config.burn_total_weekly(cfg)
    rate = costs.DEFAULT_CRYPTO_RATE

    print("=" * 74)
    print(f"  {f['name']}  —  ECONOMICS  ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})")
    print("=" * 74)
    print(f"  Venue            : {f['venue']}  ({f['mode']})")
    print(f"  Capital          : ${cap:,.2f}      Target: ${target:,.2f}  "
          f"({target/cap:.0f}x)")
    print(f"  Burn             : ${wk:.2f}/week  = ${wk/7:.2f}/day  "
          f"(${burn['vps_weekly_usd']:.0f} VPS + ${burn['tokens_weekly_usd']:.0f} tokens)")
    print(f"  Exit policy      : {f['exit_policy']}")
    print()
    print("  THE HURDLE — burn as a share of the book")
    print("  " + "-" * 70)
    for eq in HURDLE_LEVELS:
        print(f"    equity ${eq:>5}  ->  ${wk:.0f}/wk = {wk/eq*100:5.1f}% of the book per week")
    print(f"\n    Runway with ZERO edge: {cap/wk:.1f} weeks before the account is gone.")

    print(f"\n  REQUIRED RETURN — E(t+1) = E(t)*(1+r) - {wk:.0f},  ${cap:.0f} -> ${target:.0f}")
    print("  " + "-" * 70)
    print(f"    {'weekly r':>9} {'flat-line':>11} {'$'+format(cap, ',.0f')+' sits':>16} "
          f"{'weeks to $1k':>14}")
    for r in RETURN_SCENARIOS:
        flat = float("inf") if r == 0 else wk / r
        equity, weeks = cap, None
        for w in range(1, 3000):
            equity = equity * (1 + r) - wk
            if equity >= target:
                weeks = w
                break
            if equity <= 0:
                break
        sits = ("flat (never grows)" if abs(flat - cap) < 1e-6
                else ("grows" if cap > flat else "bleeds"))
        flat_disp = "infinite" if r == 0 else f"${flat:,.0f}"
        print(f"    {r*100:>8.0f}% {flat_disp:>11} {sits:>16} "
              f"{(str(weeks) if weeks else 'never (dies)'):>14}")

    print(f"\n  TRANSACTION COST — Polymarket crypto taker rate {rate} (from feeSchedule)")
    print("  " + "-" * 70)
    print(f"    {'price':>6} {'fee %stake':>12} {'round trip %stake':>18}")
    for p in COST_CURVE_PRICES:
        one_way = costs.taker_fee(1.0 / p, p, rate) * 100.0
        print(f"    {p:>6.2f} {one_way:>11.2f}% "
              f"{costs.round_trip_cost_pct(p, rate):>17.1f}%")
    print("    -> a round trip costs 2.5%-33.3% of stake: fatal for any strategy")
    print("       that trades in and out. The fund pays the taker fee exactly once")
    print("       and takes settlement, which Polymarket charges nothing for.")


def state() -> None:
    t = ledger.totals()
    positions = ledger.all_positions()
    openp = [p for p in positions if p.get("status") == "open"]
    done = [p for p in positions if p.get("status") == "settled"]

    print("\n" + "=" * 74)
    print("  LIVE STATE")
    print("=" * 74)
    print(f"  Equity           : ${t['equity_usd']:.4f}   "
          f"(start ${t['starting_equity_usd']:.2f}, "
          f"net {t['equity_usd']-t['starting_equity_usd']:+.4f})")
    print(f"  Cash             : ${t['cash_usd']:.4f}")
    print(f"  Positions value  : ${t['positions_value_usd']:.4f}  ({t['open_count']} open)")
    print(f"  Realized P&L     : ${t['realized_pnl_usd']:+.4f}")
    print(f"  Burn accrued     : ${t['burn_accrued_usd']:.4f}")

    if openp:
        print("\n  OPEN POSITIONS")
        print(f"    {'slug':<44} {'stake':>7} {'cost/sh':>8} {'model p':>8} {'edge':>7}")
        for p in openp:
            print(f"    {(p.get('slug') or '')[:44]:<44} {p['all_in_usd']:>7.2f} "
                  f"{p['cost_per_share']:>8.4f} {p['model_prob']:>8.4f} "
                  f"{p.get('edge', {}).get('edge_prob', 0):>7.4f}")
    if done:
        print("\n  SETTLED TRADES")
        print(f"    {'slug':<44} {'stake':>7} {'won':>5} {'pnl $':>9} {'pnl %':>8}")
        for p in done:
            print(f"    {(p.get('slug') or '')[:44]:<44} {p['all_in_usd']:>7.2f} "
                  f"{str(p.get('won')):>5} {p.get('pnl_usd', 0):>9.4f} "
                  f"{p.get('pnl_pct', 0):>7.2f}%")
    if not positions:
        print("\n  No positions booked yet.")

    cycle_path = os.path.join(HERE, "state", "last_cycle.json")
    if os.path.exists(cycle_path):
        with open(cycle_path) as fh:
            c = json.load(fh)
        print(f"\n  LAST CYCLE {c['ts'][:19]}  mode={c['survival_mode']}  "
              f"audit={'PASS' if c['audit']['passed'] else 'FAIL'}")
        print(f"    funnel: {c['counts']}")
    print()


if __name__ == "__main__":
    economics()
    state()
    print(calibration.render())
