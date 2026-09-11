"""
Fund cycle — the manager's orchestration of the four agents.

Order matters:
    1. accrue burn        (money leaves before anyone decides anything)
    2. settle resolved    (the oracle speaks first; free the cash)
    3. SCOUT              (what is even tradeable?)
    4. FORECASTER         (what is it worth?)
    5. RISK               (how much, and does it clear costs?)
    6. AUDITOR            (prove the above actually happened as recorded)

Nothing is booked until the cost hurdle clears. Nothing is exited early.
"""

import json
import os
import sys

import auditor
import calibration
import forecaster
import ledger
import risk
import scout
import smile

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
LAST_CYCLE = os.path.join(HERE, "state", "last_cycle.json")


def load_cfg() -> dict:
    with open(CFG_PATH) as fh:
        return json.load(fh)


def settle_resolved(cfg: dict) -> list[dict]:
    """Ask the real oracle which open positions have resolved. Only act when
    the market is closed AND its outcome prices are decisive."""
    import venue

    settled = []
    for pos in ledger.open_positions():
        try:
            m = venue.settled_market(pos["market_id"])
        except Exception as exc:                      # noqa: BLE001
            settled.append({"id": pos["id"], "error": str(exc)[:80]})
            continue

        if str(m.get("closed")).lower() != "true":
            continue
        raw = m.get("outcomePrices")
        try:
            prices = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        except (json.JSONDecodeError, TypeError):
            continue
        if len(prices) < 1:
            continue
        yes_price = float(prices[0])
        if not (yes_price > 0.99 or yes_price < 0.01):
            continue                                  # closed but not yet decisive

        won = yes_price > 0.5                          # index 0 == our "Yes" outcome
        evidence = {
            "closed": True,
            "outcomePrices": prices,
            "umaResolutionStatuses": m.get("umaResolutionStatuses"),
            "settled_market_slug": m.get("slug"),
        }
        settled.append(ledger.settle_position(pos, won, evidence))
    return settled


def run(dry_run: bool = False) -> dict:
    cfg = load_cfg()
    ledger.ensure_state()

    burned = ledger.accrue_burn(apply=not dry_run)
    settled = [] if dry_run else settle_resolved(cfg)

    candidates, scout_rejects = scout.scan(cfg)
    priced, forecast_rejects = forecaster.forecast_all(candidates)
    anchored, anchor_skips = smile.anchor(priced)
    approved, risk_rejects = risk.evaluate(anchored, cfg)
    booked = [] if dry_run else risk.execute(approved, cfg, dry_run=dry_run)

    # Calibration runs on EVERY priced market, traded or not. The fund may take
    # no trades for weeks; that must not also mean no evidence about the model.
    recorded = calibration.record(anchored)
    graded = calibration.score() if not dry_run else {"graded": 0}

    audit = auditor.audit(cfg)
    totals = ledger.totals()

    summary = {
        "ts": ledger.now_iso(),
        "burn_accrued_usd": round(burned, 6),
        "settled": [{"slug": s.get("slug"), "won": s.get("won"),
                     "pnl_usd": s.get("pnl_usd")} for s in settled],
        "counts": {
            "candidates": len(candidates),
            "scout_rejected": len(scout_rejects),
            "priced": len(priced),
            "forecast_rejected": len(forecast_rejects),
            "anchored": len(anchored),
            "anchor_skipped": len(anchor_skips),
            "approved": len(approved),
            "risk_rejected": len(risk_rejects),
            "booked": len(booked),
            "predictions_recorded": recorded,
            "predictions_graded": graded.get("graded", 0),
        },
        "calibration": calibration.summary(),
        "top_approved": [
            {"slug": a["slug"], "ev_on_stake_pct": round(a["edge"]["ev_on_stake_pct"], 2),
             "edge_prob": round(a["edge"]["edge_prob"], 4),
             "p_trade": round(a["forecast"]["p_trade"], 4),
             "sigma_market": round(a["forecast"]["sigma_market_median"], 4),
             "sigma_realized": round(a["forecast"]["sigma_realized"], 4),
             "cost_per_share": round(a["edge"]["cost_per_share"], 4)}
            for a in approved[:5]
        ],
        "survival_mode": risk.survival_mode(totals["equity_usd"], cfg),
        "totals": totals,
        "audit": {"passed": audit["passed"], "checks": audit["checks"],
                  "findings": len(audit["findings"]),
                  "warnings": len(audit.get("warnings") or [])},
    }
    os.makedirs(os.path.dirname(LAST_CYCLE), exist_ok=True)
    with open(LAST_CYCLE, "w") as fh:
        json.dump(summary, fh, indent=2)

    # Append the rejection ledger so "no edge found" is falsifiable later.
    # Capped: ~1,500 rejections per cycle would otherwise grow without bound.
    rej_path = os.path.join(HERE, "state", "rejections.jsonl")
    cap = cfg["scan"].get("rejections_max_bytes", 5_000_000)
    if os.path.exists(rej_path) and os.path.getsize(rej_path) > cap:
        os.replace(rej_path, rej_path + ".1")          # keep one generation
    with open(rej_path, "a") as fh:
        for r in scout_rejects:
            fh.write(json.dumps({"ts": summary["ts"], "stage": "scout", **r}) + "\n")
        for r in forecast_rejects:
            fh.write(json.dumps({"ts": summary["ts"], "stage": "forecast", **r}) + "\n")
        for r in risk_rejects:
            fh.write(json.dumps({"ts": summary["ts"], "stage": "risk",
                                 "slug": r["slug"], "reasons": r["reasons"]}) + "\n")
    return summary


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = run(dry_run=dry)
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["audit"]["passed"] else 1)
