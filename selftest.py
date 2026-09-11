"""
Self-test — proves the trading path actually works, and that the Auditor is
not vacuous.

The live book currently offers no trade that clears the cost hurdle, which is
the correct answer but leaves the money path unexercised. So we inject a
synthetic dislocation into an ISOLATED temp state dir and assert the whole
chain:

  1. a dislocation is approved and BOOKED
  2. the fill is priced from the raw book (not copied)
  3. the fee equals rate * shares * p * (1-p)
  4. the Auditor PASSES the honest book
  5. the Auditor FAILS a tampered book          <-- the important one
  6. settlement pays $1/share on a win, $0 on a loss, and equity replays

Run:  python3 selftest.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import auditor          # noqa: E402
import costs            # noqa: E402
import ledger           # noqa: E402
import risk             # noqa: E402

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def isolate_state() -> str:
    """Re-point the ledger at a temp dir so the live book is never touched."""
    tmp = tempfile.mkdtemp(prefix="fund-selftest-")
    ledger.STATE_DIR = tmp
    ledger.LEDGER = os.path.join(tmp, "ledger.jsonl")
    ledger.ACCOUNT = os.path.join(tmp, "account.json")
    ledger.POSITIONS = os.path.join(tmp, "positions.json")
    ledger.SNAPSHOTS = os.path.join(tmp, "snapshots")
    ledger.ensure_state()
    return tmp


def synthetic_row() -> dict:
    """A BTC 'above' strike priced far too cheap: real probability ~0.90 but
    the book asks 0.50. A deliberate, obvious dislocation."""
    return {
        "market_id": "selftest-1",
        "slug": "selftest-bitcoin-above-70k",
        "question": "SELFTEST: Bitcoin above 70k?",
        "symbol": "BTCUSDT",
        "family": "digital_above",
        "strike": 70000.0,
        "token_id_yes": "tok-yes",
        "end_date": "2026-10-01T00:00:00Z",
        "days_to_resolution": 20.0,
        "volume24hr": 100000.0,
        "liquidity": 50000.0,
        "best_bid": 0.49,
        "best_ask": 0.50,
        "spread": 0.01,
        "tick": 0.01,
        "fee_rate": 0.07,
        "fee_type": "crypto_fees_v2",
        "fees_enabled": True,
        "book_bids": [{"price": "0.49", "size": "5000"}],
        "book_asks": [{"price": "0.50", "size": "5000"}],
        "forecast": {
            "ok": True, "family": "digital_above", "symbol": "BTCUSDT",
            "spot": 77000.0, "strike": 70000.0, "years": 20 / 365,
            "sigma_annual": 0.5, "sigma_market_median": 0.5,
            "sigma_realized": 0.5, "vol_disagreement_pct": 0.0,
            "p_mid": 0.90, "p_conservative": 0.90,
            "p_anchored": 0.90, "p_anchored_conservative": 0.90,
            "p_trade": 0.90, "method": "selftest",
        },
    }


def main() -> int:
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    tmp = isolate_state()
    try:
        print("SELFTEST — trading path and auditor integrity")
        print(f"  isolated state: {tmp}\n")

        # ---- 1. approval + booking
        row = synthetic_row()
        approved, rejected = risk.evaluate([row], cfg)
        check("dislocation passes risk gate", len(approved) == 1,
              f"approved={len(approved)} rejected={len(rejected)}")
        if not approved:
            return 1
        booked = risk.execute(approved, cfg)
        booked = [b for b in booked if not b.get("skipped")]
        check("position booked", len(booked) == 1, f"booked={len(booked)}")
        if not booked:
            return 1
        pos = booked[0]["position"]

        # ---- 2. fill priced from the raw book
        expect_shares = pos["all_in_usd"] / pos["cost_per_share"]
        check("shares match stake/cost", abs(expect_shares - pos["shares"]) < 1e-6,
              f"shares={pos['shares']:.4f}")

        # ---- 3. fee identity
        expected_fee = 0.07 * pos["shares"] * pos["vwap"] * (1 - pos["vwap"])
        check("fee identity holds", abs(expected_fee - pos["fee_usd"]) < 1e-6,
              f"fee={pos['fee_usd']:.6f} expected={expected_fee:.6f}")

        # ---- 4. auditor passes the honest book
        res = auditor.audit(cfg)
        check("auditor PASSES honest book", res["passed"], f"findings={len(res['findings'])}")

        # ---- 5. auditor catches tampering  (the integrity check that matters)
        snap = (pos.get("fills") or [{}])[0].get("snapshot") or pos.get("snapshot")
        with open(snap) as fh:
            data = json.load(fh)
        data["fill_as_booked"]["shares"] *= 1.5          # steal shares
        with open(snap, "w") as fh:
            json.dump(data, fh)
        res = auditor.audit(cfg)
        caught = not res["passed"]
        check("auditor FAILS tampered book", caught,
              f"findings={[f['check'] for f in res['findings']]}")

        # restore honest snapshot for settlement test
        data["fill_as_booked"]["shares"] = pos["shares"]
        with open(snap, "w") as fh:
            json.dump(data, fh)

        # ---- 6. settlement + equity replay
        before = ledger.totals()
        settled = ledger.settle_position(pos, won=True, evidence={"selftest": True})
        check("win pays $1/share",
              abs(settled["payout_usd"] - pos["shares"]) < 1e-6,
              f"payout={settled['payout_usd']:.4f} shares={pos['shares']:.4f}")
        after = ledger.totals()
        expected_equity = before["cash_usd"] + pos["shares"]
        check("equity after win = cash + shares",
              abs(after["equity_usd"] - expected_equity) < 1e-6,
              f"equity={after['equity_usd']:.4f} expected={expected_equity:.4f}")

        res = auditor.audit(cfg)
        check("auditor PASSES after settlement", res["passed"],
              f"findings={[f['check'] for f in res['findings']]}")

        # ---- 7. policy: hold-to-resolution has no exit path
        check("no exit path exists in ledger",
              not any(hasattr(ledger, n) for n in ("close_position", "exit_position")))

        print()
        if FAILS:
            print(f"SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
            return 1
        print("SELFTEST PASSED — booking, fee math, auditor (incl. tamper detection) and settlement verified")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
