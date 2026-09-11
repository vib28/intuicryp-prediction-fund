"""
Report — the economics, always with transaction costs priced in.

Run:  python3 report.py
"""

import json
import os
import sys
from datetime import datetime, timezone

import costs
import ledger

HERE = os.path.dirname(os.path.abspath(__file__))


def economics(cfg: dict) -> None:
    f, burn = cfg["fund"], cfg["burn"]
    cap, target = float(f["capital_usd"]), float(f["target_usd"])
    wk = float(burn["total_weekly_usd"])

    print("=" * 74)
    print(f"  {f['name']}  —  ECONOMICS  ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})")
    print("=" * 74)
    print(f"  Venue            : {f['venue']}  ({f['mode']})")
    print(f"  Capital          : ${cap:,.2f}      Target: ${target:,.2f}  ({target/cap:.0f}x)")
    print(f"  Burn             : ${wk:.2f}/week  = ${wk/7:.2f}/day  "
          f"(${burn['vps_weekly_usd']:.0f} VPS + ${burn['tokens_weekly_usd']:.0f} tokens)")
    print(f"  Exit policy      : {f['exit_policy']}")
    print()
    print("  THE HURDLE — burn as a share of the book")
    print("  " + "-" * 70)
    for eq in (100, 150, 250, 500, 750, 1000):
        print(f"    equity ${eq:>5}  ->  ${wk:.0f}/wk = {wk/eq*100:5.1f}% of the book per week")
    print(f"\n    Runway with ZERO edge: {cap/wk:.1f} weeks before the account is gone.")

    print("\n  REQUIRED RETURN — E(t+1) = E(t)*(1+r) - 15,  $100 -> $1,000")
    print("  " + "-" * 70)
    print(f"    {'weekly r':>9} {'flat-line':>11} {'$100 sits':>16} {'weeks to $1k':>14}")
    for r in (0.0, 0.05, 0.10, 0.15, 0.18, 0.20, 0.25, 0.30, 0.40, 0.50):
        flat = float("inf") if r == 0 else wk / r
        e, n = cap, None
        for w in range(1, 3000):
            e = e * (1 + r) - wk
            if e >= target:
                n = w
                break
            if e <= 0:
                break
        sits = "flat (never grows)" if abs(flat - cap) < 1e-9 else ("grows" if cap > flat else "bleeds")
        print(f"    {r*100:>8.0f}% {'$'+format(flat,',.0f') if r else 'infinite':>11} "
              f"{sits:>16} {(str(n) if n else 'never (dies)'):>14}")

    print("\n  TRANSACTION COST — Polymarket crypto taker rate 0.07 (from feeSchedule)")
    print("  " + "-" * 70)
    print(f"    {'price':>6} {'fee %stake':>12} {'round trip %stake':>18}")
    for p in (0.05, 0.10, 0.20, 0.30, 0.50, 0.70, 0.90):
        stake = 1.0
        shares = stake / p
        fee = costs.taker_fee(shares, p)                  # one way, as % of stake
        spread_cost = 0.01 * shares
        rt = (2 * fee + spread_cost) / stake * 100
        print(f"    {p:>6.2f} {fee / stake * 100:>11.2f}% {rt:>17.1f}%")
    print("    -> a round trip costs 2.5%-33.3% of stake: fatal for any strategy")
    print("       that trades in and out. The fund pays the taker fee exactly once")
    print("       and takes settlement, which Polymarket charges nothing for.")


def state(cfg: dict) -> None:
    t = ledger.totals()
    pos = ledger.all_positions()
    openp = [p for p in pos if p.get("status") == "open"]
    done = [p for p in pos if p.get("status") == "settled"]

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
                  f"{p.get('edge',{}).get('edge_prob',0):>7.4f}")
    if done:
        print("\n  SETTLED TRADES")
        print(f"    {'slug':<44} {'stake':>7} {'won':>5} {'pnl $':>9} {'pnl %':>8}")
        for p in done:
            print(f"    {(p.get('slug') or '')[:44]:<44} {p['all_in_usd']:>7.2f} "
                  f"{str(p.get('won')):>5} {p.get('pnl_usd',0):>9.4f} "
                  f"{p.get('pnl_pct',0):>7.2f}%")
    if not pos:
        print("\n  No positions booked yet.")

    lc = os.path.join(HERE, "state", "last_cycle.json")
    if os.path.exists(lc):
        with open(lc) as fh:
            c = json.load(fh)
        print(f"\n  LAST CYCLE {c['ts'][:19]}  mode={c['survival_mode']}  "
              f"audit={'PASS' if c['audit']['passed'] else 'FAIL'}")
        print(f"    funnel: {c['counts']}")
    print()


if __name__ == "__main__":
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    economics(cfg)
    state(cfg)
