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
LAST_CYCLE = os.path.join(HERE, "state", "last_cycle.json")
CALIB_MARKER = os.path.join(HERE, "state", ".calibration_announced")

# Say something about calibration once a real sample exists, then re-announce
# only when the sample has grown by another whole band.
CALIB_MIN_ROWS = 50
CALIB_BAND = 100


def _calibration_message(cal: dict) -> str | None:
    """Announce calibration once there is enough evidence to mean it.

    Reads the ONE-PER-MARKET sample, not the raw row count: rows are correlated
    (a market alive for 3 days contributes up to 6 rows sharing one outcome), so
    rows inflate the apparent sample size and would announce confidence the data
    does not support.

    This used to read `calibration["n"] / ["skill"] / ["brier"]`, none of which
    exist at the top level of `calibration.summary()` — the real values are
    nested under `one_per_market`. So the notification could never fire: it
    looked implemented and was dead.
    """
    per_market = cal.get("one_per_market") or {}
    n = int(per_market.get("n") or 0)
    if n < CALIB_MIN_ROWS:
        return None

    announced = 0
    if os.path.exists(CALIB_MARKER):
        try:
            announced = int(open(CALIB_MARKER).read().strip() or 0)
        except ValueError:
            announced = 0
    band = (n // CALIB_BAND) * CALIB_BAND
    if band <= announced:
        return None

    with open(CALIB_MARKER, "w") as fh:
        fh.write(str(band))
    skill = float(per_market.get("skill") or 0.0)
    brier = float(per_market.get("brier") or 0.0)
    return (f"📊 CALIBRATION: {n} settled markets — Brier {brier:.4f}, "
            f"skill vs coin flip {skill:+.4f} "
            f"({'better' if skill > 0 else 'WORSE'})")


def main() -> int:
    if not os.path.exists(LAST_CYCLE):
        print("FUND: no cycle has run yet")
        return 0
    with open(LAST_CYCLE) as fh:
        d = json.load(fh)

    msgs = []
    audit = d.get("audit") or {}
    if not audit.get("passed"):
        msgs.append(f"🚨 AUDIT FAILURE — {audit.get('findings', 0)} finding(s)")
    for s in d.get("settled") or []:
        if s.get("pnl_usd") is not None:
            msgs.append(f"{'✅' if (s['pnl_usd'] or 0) > 0 else '❌'} SETTLED "
                        f"{s.get('slug')} pnl ${s['pnl_usd']:+.4f}")
    if (d.get("counts") or {}).get("booked"):
        msgs.append(f"📌 BOOKED {d['counts']['booked']} position(s)")
        for a in d.get("top_approved") or []:
            msgs.append(f"   {a['slug']}  p={a['p_trade']} vs cost {a['cost_per_share']} "
                        f"(EV {a['ev_on_stake_pct']}%)")

    mode = d.get("survival_mode")
    if mode != "normal":
        msgs.append(f"{'🛑' if mode == 'dead' else '⚠️'} SURVIVAL MODE: {str(mode).upper()}")

    try:
        note = _calibration_message(d.get("calibration") or {})
        if note:
            msgs.append(note)
    except Exception as exc:                            # noqa: BLE001
        # Report rather than swallow: a silently failing notification is how the
        # dead calibration message above stayed dead.
        msgs.append(f"⚠️ calibration notification failed: {type(exc).__name__}: {exc}")

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
