"""
One-off migration: collapse duplicate open positions for the same market into a
single position holding multiple fills.

Why: the ledger previously appended a new position row per fill, so two fills on
one market appeared as two positions. On Polymarket there is one net balance per
outcome token — repeat orders merge — so the book must too, otherwise the
exposure cap is measured per fill rather than per market.

Idempotent: positions already carrying a `fills` list are left alone, and
already-merged markets are not merged twice.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ledger  # noqa: E402


def to_fills(p: dict) -> list[dict]:
    """Normalise a legacy single-fill row into the fills list shape."""
    if p.get("fills"):
        return p["fills"]
    return [{
        "ts": p.get("opened"),
        "shares": p["shares"],
        "vwap": p["vwap"],
        "all_in_usd": p["all_in_usd"],
        "fee_usd": p["fee_usd"],
        "stake_usd": float(p["all_in_usd"]) - float(p["fee_usd"]),
        "model_prob": p.get("model_prob"),
        "snapshot": p.get("snapshot"),
    }]


def main() -> int:
    state = ledger.load_json(ledger.POSITIONS, {"positions": []})
    before = len(state["positions"])
    merged: dict[str, dict] = {}
    order: list[dict] = []

    for p in state["positions"]:
        if p.get("status") != "open":
            order.append(p)
            continue
        mid = str(p.get("market_id"))
        if mid in merged:
            merged[mid]["fills"].extend(to_fills(p))
            ledger.append_event("migrate_merge", id=merged[mid]["id"], market_id=mid)
            print(f"  merged duplicate position for {mid}")
        else:
            p["fills"] = to_fills(p)
            merged[mid] = p
            order.append(p)

    for p in merged.values():
        ledger._recompute(p)

    state["positions"] = order
    ledger.save_json(ledger.POSITIONS, state)
    print(f"positions: {before} -> {len(order)}")
    for p in merged.values():
        print(f"  {p['id']} {str(p.get('slug'))[:44]} "
              f"fills={len(p['fills'])} shares={p['shares']:.2f} "
              f"cost/share={p['cost_per_share']:.4f} all_in={p['all_in_usd']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
