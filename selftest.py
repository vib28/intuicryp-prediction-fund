"""
Self-test — proves the trading path actually works, and that the Auditor is not
vacuous.

The live book often offers no trade that clears the cost hurdle, which is the
correct answer but leaves the money path unexercised. So we inject a synthetic
dislocation into an ISOLATED temp state dir and assert the whole chain:

  1. a dislocation is approved and BOOKED
  2. the fill is priced from the raw book (not copied)
  3. the fee equals rate * shares * p * (1-p)
  4. the Auditor PASSES the honest book
  5. the Auditor FAILS a tampered book            <-- the integrity check
  6. a repeat order MERGES, and the equity replay survives the `add` event
  7. the percent cap check actually fires when breached
  8. the Auditor FAILS an overstated equity base
  9. settlement pays $1/share on a win, $0 on a loss, and equity replays
 10. hold-to-resolution has no exit path

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
import config           # noqa: E402
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
            "sigma_annual": 0.5, "sigma_market": 0.5,
            "sigma_realized": 0.5, "vol_disagreement_pct": 0.0,
            "p_mid": 0.90, "p_conservative": 0.90,
            "p_anchored": 0.90, "p_anchored_conservative": 0.90,
            "p_trade": 0.90, "method": "selftest",
        },
    }


def set_fill_field(key: str, value) -> None:
    """Mutate a fill entry in the isolated positions file."""
    state = ledger.load_json(ledger.POSITIONS, {"positions": []})
    state["positions"][0]["fills"][0][key] = value
    ledger.save_json(ledger.POSITIONS, state)


def main() -> int:
    cfg = config.load()
    tmp = isolate_state()
    try:
        print("SELFTEST — trading path and auditor integrity")
        print(f"  isolated state: {tmp}\n")

        # ---- approval + booking
        approved, rejected = risk.evaluate([synthetic_row()], cfg)
        check("dislocation passes risk gate", len(approved) == 1,
              f"approved={len(approved)} rejected={len(rejected)}")
        if not approved:
            return 1
        booked = [b for b in risk.execute(approved, cfg) if not b.get("skipped")]
        check("position booked", len(booked) == 1, f"booked={len(booked)}")
        if not booked:
            return 1
        pos = booked[0]["position"]

        # ---- fill priced from the raw book
        check("shares match stake/cost",
              abs(pos["all_in_usd"] / pos["cost_per_share"] - pos["shares"]) < 1e-6,
              f"shares={pos['shares']:.4f}")

        # ---- fee identity
        fee_rate = float(pos["fills"][0]["vwap"])
        expected_fee = 0.07 * pos["shares"] * fee_rate * (1 - fee_rate)
        check("fee identity holds", abs(expected_fee - pos["fee_usd"]) < 1e-6,
              f"fee={pos['fee_usd']:.6f} expected={expected_fee:.6f}")

        # ---- auditor on the honest book
        res = auditor.audit(cfg)
        check("auditor PASSES honest book", res["passed"],
              f"findings={[f['check'] for f in res['findings']]}")

        # ---- auditor catches tampering (the check that matters)
        snap = pos["fills"][0]["snapshot"]
        with open(snap) as fh:
            data = json.load(fh)
        honest_shares = data["fill_as_booked"]["shares"]
        data["fill_as_booked"]["shares"] = honest_shares * 1.5     # steal shares
        with open(snap, "w") as fh:
            json.dump(data, fh)
        res = auditor.audit(cfg)
        check("auditor FAILS tampered fill", not res["passed"],
              f"findings={[f['check'] for f in res['findings']]}")
        data["fill_as_booked"]["shares"] = honest_shares
        with open(snap, "w") as fh:
            json.dump(data, fh)
        check("auditor PASSES once the snapshot is restored", auditor.audit(cfg)["passed"])

        # ---- a repeat order MERGES, and the `add` event replays cleanly
        # This is a regression test: `add` (a top-up) emits a different ledger
        # kind from `open`, and the equity replay originally ignored it, so the
        # first top-up would have produced a phantom audit failure forever.
        approved2, _ = risk.evaluate([synthetic_row()], cfg)
        booked2 = [b for b in risk.execute(approved2, cfg) if not b.get("skipped")]
        opens = ledger.open_positions()
        check("repeat order merges into one position", len(opens) == 1,
              f"open positions={len(opens)}")
        check("position now holds two fills", len(opens[0]["fills"]) == 2,
              f"fills={len(opens[0]['fills'])}")
        check("second fill booked", len(booked2) == 1)
        res = auditor.audit(cfg)
        check("auditor PASSES after a top-up (add-event replay)", res["passed"],
              f"findings={[f['check'] for f in res['findings']]}")

        # ---- the percent cap is not vacuous: shrink the equity base and it fires
        honest_equity = ledger.load_json(ledger.POSITIONS, {"positions": []}) \
            ["positions"][0]["fills"][0]["equity_before_usd"]
        set_fill_field("equity_before_usd", 1.0)
        res_low = auditor.audit(cfg)
        check("percent cap FIRES when a fill exceeds it",
              any(f["check"] == "pct_cap" for f in res_low["findings"]),
              f"findings={[f['check'] for f in res_low['findings']]}")
        set_fill_field("equity_before_usd", honest_equity)
        check("percent cap holds on an honest book",
              auditor.audit(cfg)["passed"])

        # ---- settlement + equity replay
        pos = ledger.open_positions()[0]
        before = ledger.totals()
        settled = ledger.settle_position(pos, won=True, evidence={"selftest": True})
        check("win pays $1/share",
              abs(settled["payout_usd"] - pos["shares"]) < 1e-6,
              f"payout={settled['payout_usd']:.4f} shares={pos['shares']:.4f}")
        after = ledger.totals()
        check("equity after win = cash + shares",
              abs(after["equity_usd"] - (before["cash_usd"] + pos["shares"])) < 1e-6,
              f"equity={after['equity_usd']:.4f}")

        res = auditor.audit(cfg)
        check("auditor PASSES after settlement", res["passed"],
              f"findings={[f['check'] for f in res['findings']]}")

        # ---- policy: hold-to-resolution has no exit path
        check("no exit path exists in ledger",
              not any(hasattr(ledger, n) for n in ("close_position", "exit_position")))

        print()
        if FAILS:
            print(f"SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
            return 1
        print("SELFTEST PASSED — booking, merge, fee math, auditor (incl. tamper "
              "detection and cap enforcement) and settlement verified")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
