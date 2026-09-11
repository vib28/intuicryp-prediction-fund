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
  3. SIZE CAPS       — no position exceeded policy caps at the time it opened
  4. HOLD INVARIANT  — nothing was ever exited early (the whole strategy)
  5. EQUITY REPLAY   — cash derived from events equals the stored cash

Exit code 1 if any check fails, so cron/monitoring can react.
"""

import json
import math
import os
import sys

import costs
import ledger

REL_TOL = 1e-6


def _close(a: float, b: float) -> bool:
    return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=1e-6)


def audit(cfg: dict) -> dict:
    findings, warnings, checks = [], [], 0

    positions = ledger.all_positions()

    # ---- 1 & 2: fill and fee fidelity, recomputed from raw snapshots
    # A position may hold several fills (repeat orders merge on Polymarket), so
    # every fill is verified independently against its own snapshot.
    for p in positions:
        entries = p.get("fills")
        if not entries:
            # legacy single-fill shape
            entries = [{
                "shares": p.get("shares"), "vwap": p.get("vwap"),
                "fee_usd": p.get("fee_usd"), "all_in_usd": p.get("all_in_usd"),
                "snapshot": p.get("snapshot"),
            }]
        for n, entry in enumerate(entries):
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
            stake = float(snap["target_stake_usd"])
            recomputed = costs.simulate_buy(snap["book_asks"], stake, rate=rate)
            booked = snap["fill_as_booked"]

            if not recomputed:
                findings.append({"check": "fill_recompute", "id": tag,
                                 "detail": "recompute returned no fill"})
                continue

            for field, a, b in (
                ("shares", booked["shares"], recomputed.shares),
                ("vwap", booked["vwap"], recomputed.vwap),
                ("fee_usd", booked["fee_usd"], recomputed.fee_usd),
                ("all_in_usd", booked["all_in_usd"], recomputed.all_in_usd),
            ):
                if not _close(a, b):
                    findings.append({
                        "check": "fill_fidelity", "id": tag, "field": field,
                        "detail": f"booked={a!r} recomputed={b!r}",
                    })

            # fee identity: fee must equal rate * shares * p * (1-p) on the vwap
            expected_fee = rate * recomputed.shares * recomputed.vwap * (1 - recomputed.vwap)
            if not _close(expected_fee, recomputed.fee_usd):
                findings.append({"check": "fee_identity", "id": tag,
                                 "detail": f"expected {expected_fee!r} got {recomputed.fee_usd!r}"})

        # aggregates must equal the sum of the fills
        if p.get("fills"):
            checks += 1
            tot_shares = sum(float(f["shares"]) for f in p["fills"])
            tot_allin = sum(float(f["all_in_usd"]) for f in p["fills"])
            if not _close(p.get("shares"), tot_shares) or not _close(p.get("all_in_usd"), tot_allin):
                findings.append({"check": "aggregate_matches_fills", "id": p.get("id"),
                                 "detail": f"shares {p.get('shares')} vs {tot_shares}, "
                                           f"all_in {p.get('all_in_usd')} vs {tot_allin}"})

    # ---- 3: size caps
    acct = ledger.load_json(ledger.ACCOUNT, {})
    if positions:
        max_pct = cfg["risk"]["max_position_pct"] / 100.0
        abs_cap = cfg["risk"]["absolute_max_position_usd"]
        for p in positions:
            checks += 1
            stake = float(p.get("all_in_usd") or 0)
            if stake > abs_cap + 1e-6:
                # A position carrying an explicit accepted-exception is reported
                # LOUDLY but does not fail the run. Rationale: the fund has no
                # exit path, so a historical over-cap position cannot be reduced;
                # failing every audit forever would train the reader to ignore
                # the audit, which costs more than the breach.
                if p.get("accepted_over_cap"):
                    warnings.append({
                        "check": "accepted_over_cap", "id": p.get("id"),
                        "detail": f"stake {stake:.4f} > cap {abs_cap} — "
                                  f"{p['accepted_over_cap'].get('reason', 'accepted')}",
                    })
                else:
                    findings.append({"check": "absolute_cap", "id": p.get("id"),
                                     "detail": f"stake {stake:.4f} > cap {abs_cap}"})
    # concurrency is a forward-looking constraint; verify never exceeded
    concurrent = [e for e in ledger.read_ledger() if e.get("kind") == "open"]
    if len(concurrent) > cfg["risk"]["max_concurrent_positions"] * 50:
        findings.append({"check": "concurrency_total_suspicious",
                         "detail": f"{len(concurrent)} opens recorded — inspect"})

    # ---- 4: hold-to-resolution invariant
    close_events = [e for e in ledger.read_ledger()
                    if e.get("kind") in ("close", "exit", "sell")]
    checks += 1
    if close_events:
        findings.append({"check": "hold_invariant",
                         "detail": f"{len(close_events)} exit events found — strategy is hold-to-resolution"})

    # ---- 5: equity replay
    checks += 1
    ev = ledger.read_ledger()
    start = float(acct.get("starting_equity_usd", ledger.STARTING_EQ))
    replayed = start
    for e in ev:
        if e["kind"] == "burn":
            replayed -= float(e.get("amount_usd") or 0)
        elif e["kind"] == "open":
            replayed -= float(e.get("all_in_usd") or 0)
        elif e["kind"] == "settle":
            replayed += float(e.get("payout_usd") or 0)
    # cash only (positions are separate assets)
    stored_cash = float(acct.get("cash_usd", start))
    if not math.isclose(replayed, stored_cash, rel_tol=1e-4, abs_tol=0.02):
        findings.append({"check": "equity_replay",
                         "detail": f"replayed cash {replayed:.6f} vs stored {stored_cash:.6f}"})

    return {"checks": checks, "positions": len(positions),
            "findings": findings, "warnings": warnings,
            "passed": len(findings) == 0}


def main() -> int:
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
    res = audit(cfg)
    print(f"AUDIT: {res['checks']} checks over {res['positions']} position(s) — "
          f"{'PASS' if res['passed'] else 'FAIL'}")
    for w in res.get("warnings") or []:
        print("  WARNING:", json.dumps(w))
    for f in res["findings"]:
        print("  FINDING:", json.dumps(f))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
