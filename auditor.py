"""
AGENT 4 — AUDITOR
Mandate: assume the other three are wrong and try to prove it.

This agent never takes a position and never talks to the traders. It reads the
SAME evidence they used (the stored order-book snapshot) and independently
recomputes the fill, the fee, the edge, and the equity. Any disagreement is a
finding.

Checks:
  1. FILL FIDELITY   — recompute shares/vwap/fee/all-in from the raw snapshot
  2. FEE FIDELITY    — the rate charged equals the market's own feeSchedule.rate
  3. AGGREGATES      — a position's totals equal the sum of its fills
  4. SIZE CAPS       — absolute cap per position, and the percent cap per fill
                       measured against equity as it stood at that moment
  5. HOLD INVARIANT  — nothing was ever exited early (the whole strategy)
  6. EQUITY REPLAY   — cash derived from the event log equals the stored cash

Findings fail the run (exit code 1) so cron and monitoring can react. Warnings
are reported loudly but do not fail, and exist for exactly one situation: a
breach that is real, already recorded, and impossible to undo — see
`_verify_caps`.
"""

import json
import math
import os
import sys

import config
import costs
import ledger

REL_TOL = 1e-6


def _close(a: float, b: float) -> bool:
    return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=1e-6)


def _fills_of(pos: dict) -> list[dict]:
    """A position's fills, normalising the pre-merge single-fill shape."""
    entries = pos.get("fills")
    if entries:
        return entries
    return [{
        "shares": pos.get("shares"), "vwap": pos.get("vwap"),
        "fee_usd": pos.get("fee_usd"), "all_in_usd": pos.get("all_in_usd"),
        "snapshot": pos.get("snapshot"),
    }]


def _verify_fills(positions: list[dict], findings: list) -> int:
    """Recompute every fill from its own frozen snapshot, and check aggregates."""
    checks = 0
    for p in positions:
        for n, entry in enumerate(_fills_of(p)):
            tag = f"{p.get('id')}[fill {n}]"
            snap_path = entry.get("snapshot")
            if not snap_path or not os.path.exists(snap_path):
                findings.append({"check": "snapshot_present", "id": tag,
                                 "detail": "snapshot missing — fill cannot be verified"})
                continue
            with open(snap_path) as fh:
                snap = json.load(fh)

            checks += 1
            rate = float(snap["market"].get("fee_rate") or costs.DEFAULT_CRYPTO_RATE)
            recomputed = costs.simulate_buy(snap["book_asks"],
                                            float(snap["target_stake_usd"]), rate=rate)
            if not recomputed:
                findings.append({"check": "fill_recompute", "id": tag,
                                 "detail": "recompute returned no fill"})
                continue

            booked = snap["fill_as_booked"]
            for field, was, now in (
                ("shares", booked["shares"], recomputed.shares),
                ("vwap", booked["vwap"], recomputed.vwap),
                ("fee_usd", booked["fee_usd"], recomputed.fee_usd),
                ("all_in_usd", booked["all_in_usd"], recomputed.all_in_usd),
            ):
                if not _close(was, now):
                    findings.append({"check": "fill_fidelity", "id": tag,
                                     "field": field,
                                     "detail": f"booked={was!r} recomputed={now!r}"})

            # fee identity: fee must equal rate * shares * p * (1-p) at the vwap
            expected = rate * recomputed.shares * recomputed.vwap * (1 - recomputed.vwap)
            if not _close(expected, recomputed.fee_usd):
                findings.append({"check": "fee_identity", "id": tag,
                                 "detail": f"expected {expected!r} got {recomputed.fee_usd!r}"})

        if p.get("fills"):
            checks += 1
            tot_shares = sum(float(f["shares"]) for f in p["fills"])
            tot_allin = sum(float(f["all_in_usd"]) for f in p["fills"])
            if not (_close(p.get("shares"), tot_shares)
                    and _close(p.get("all_in_usd"), tot_allin)):
                findings.append({"check": "aggregate_matches_fills", "id": p.get("id"),
                                 "detail": f"shares {p.get('shares')} vs {tot_shares}, "
                                           f"all_in {p.get('all_in_usd')} vs {tot_allin}"})
    return checks


def _verify_caps(positions: list[dict], cfg: dict,
                 findings: list, warnings: list) -> int:
    """Absolute cap per position; percent cap per fill, at the time it was made."""
    max_pct = cfg["risk"]["max_position_pct"] / 100.0
    abs_cap = cfg["risk"]["absolute_max_position_usd"]
    checks = 0

    for p in positions:
        for n, entry in enumerate(_fills_of(p)):
            equity_before = entry.get("equity_before_usd")
            if equity_before is None:
                continue          # fills booked before this field existed
            checks += 1
            pct_cap = max_pct * float(equity_before)
            stake = float(entry.get("all_in_usd") or 0)
            if stake > pct_cap + 1e-6:
                findings.append({
                    "check": "pct_cap", "id": f"{p.get('id')}[fill {n}]",
                    "detail": f"fill {stake:.4f} > {max_pct:.0%} of equity "
                              f"({pct_cap:.4f}, equity {equity_before:.4f} at the time)"})

        checks += 1
        stake = float(p.get("all_in_usd") or 0)
        if stake > abs_cap + 1e-6:
            # A position carrying an explicit accepted-exception is reported
            # LOUDLY but does not fail the run. Rationale: the fund has no exit
            # path, so a historical over-cap position cannot be reduced; failing
            # every audit forever would train the reader to ignore the audit,
            # which costs more than the breach does.
            if p.get("accepted_over_cap"):
                warnings.append({
                    "check": "accepted_over_cap", "id": p.get("id"),
                    "detail": f"stake {stake:.4f} > cap {abs_cap} — "
                              f"{p['accepted_over_cap'].get('reason', 'accepted')}"})
            else:
                findings.append({"check": "absolute_cap", "id": p.get("id"),
                                 "detail": f"stake {stake:.4f} > cap {abs_cap}"})
    return checks


def _verify_hold_invariant(events: list[dict], findings: list) -> int:
    """Nothing may ever have been exited early — that is the entire strategy."""
    exits = [e for e in events if e.get("kind") in ("close", "exit", "sell")]
    if exits:
        findings.append({"check": "hold_invariant",
                         "detail": f"{len(exits)} exit events found — strategy is "
                                   f"hold-to-resolution"})
    return 1


def _verify_equity_replay(events: list[dict], acct: dict, findings: list) -> int:
    """Rebuild cash from the event log and compare with the stored cash.

    Both cash movements are replayed: `open` and `add`. Missing `add` was a live
    bug — a repeat order on a held market emits `add`, not `open`, so the very
    first top-up would have produced a phantom equity-replay failure every cycle
    from then on. `migrate_*` events move no cash and are correctly ignored.
    """
    start = float(acct.get("starting_equity_usd", config.capital()))
    replayed = start
    for e in events:
        kind = e.get("kind")
        if kind == "burn":
            replayed -= float(e.get("amount_usd") or 0)
        elif kind in ("open", "add"):
            replayed -= float(e.get("all_in_usd") or 0)
        elif kind == "settle":
            replayed += float(e.get("payout_usd") or 0)

    stored = float(acct.get("cash_usd", start))
    if not math.isclose(replayed, stored, rel_tol=1e-4, abs_tol=0.02):
        findings.append({"check": "equity_replay",
                         "detail": f"replayed cash {replayed:.6f} vs stored {stored:.6f}"})
    return 1


def audit(cfg: dict) -> dict:
    findings: list = []
    warnings: list = []
    positions = ledger.all_positions()
    events = ledger.read_ledger()
    acct = ledger.load_json(ledger.ACCOUNT, {})

    checks = 0
    checks += _verify_fills(positions, findings)
    checks += _verify_caps(positions, cfg, findings, warnings)
    checks += _verify_hold_invariant(events, findings)
    checks += _verify_equity_replay(events, acct, findings)

    # A crude sanity bound on total activity, so a runaway loop is visible.
    opens = [e for e in events if e.get("kind") == "open"]
    if len(opens) > cfg["risk"]["max_concurrent_positions"] * 50:
        findings.append({"check": "concurrency_total_suspicious",
                         "detail": f"{len(opens)} opens recorded — inspect"})

    return {"checks": checks, "positions": len(positions),
            "findings": findings, "warnings": warnings,
            "passed": len(findings) == 0}


def main() -> int:
    res = audit(config.load())
    print(f"AUDIT: {res['checks']} checks over {res['positions']} position(s) — "
          f"{'PASS' if res['passed'] else 'FAIL'}")
    for w in res["warnings"]:
        print("  WARNING:", json.dumps(w))
    for f in res["findings"]:
        print("  FINDING:", json.dumps(f))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
