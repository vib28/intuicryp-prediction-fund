#!/usr/bin/env python3
"""
notify — decide whether this cycle is worth interrupting the principal for.

Silent unless something actually happened: a booking, a settlement, a survival
ladder change, or an audit failure. "We scanned 1,500 markets and found no edge"
is the expected steady state and must NOT generate a message, or the signal
drowns in noise.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    path = os.path.join(HERE, "state", "last_cycle.json")
    if not os.path.exists(path):
        print("FUND: no cycle has run yet")
        return 0
    with open(path) as fh:
        d = json.load(fh)

    msgs = []
    if not d["audit"]["passed"]:
        msgs.append(f"🚨 AUDIT FAILURE — {d['audit']['findings']} finding(s)")
    for s in d.get("settled") or []:
        if s.get("pnl_usd") is not None:
            msgs.append(f"{'✅' if (s['pnl_usd'] or 0) > 0 else '❌'} SETTLED {s.get('slug')} "
                        f"pnl ${s['pnl_usd']:+.4f}")
    if d["counts"].get("booked"):
        msgs.append(f"📌 BOOKED {d['counts']['booked']} position(s)")
        for a in d.get("top_approved") or []:
            msgs.append(f"   {a['slug']}  p={a['p_trade']} vs cost {a['cost_per_share']} "
                        f"(EV {a['ev_on_stake_pct']}%)")
    mode = d.get("survival_mode")
    if mode != "normal":
        emoji = "🛑" if mode == "dead" else "⚠️"
        msgs.append(f"{emoji} SURVIVAL MODE: {mode.upper()}")

    # First real calibration signal: the model may take no trades for weeks, so
    # the moment we have enough graded predictions to say whether the
    # probabilities are honest, say so. Announced once per threshold.
    try:
        cal = d.get("calibration") or {}
        n = int(cal.get("n") or 0)
        if n >= 50:
            marker = os.path.join(HERE, "state", ".calibration_announced")
            announced = 0
            if os.path.exists(marker):
                announced = int(open(marker).read().strip() or 0)
            bucket = (n // 100) * 100
            if bucket > announced:
                with open(marker, "w") as fh:
                    fh.write(str(bucket))
                skill = cal.get("skill", 0.0)
                msgs.append(f"📊 CALIBRATION: {n} graded predictions — "
                            f"Brier {cal.get('brier', 0):.4f}, "
                            f"skill vs coin flip {skill:+.4f} "
                            f"({'better' if skill > 0 else 'WORSE'})")
    except Exception:                                   # noqa: BLE001
        pass

    if not msgs:
        return 0                              # silent: nothing happened

    t = d.get("totals") or {}
    print(f"[fund {d['ts'][:16]}Z]")
    print("\n".join(msgs))
    print(f"equity ${t.get('equity_usd', 0):.4f}  "
          f"open {t.get('open_count', 0)}  real ${t.get('realized_pnl_usd', 0):+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
