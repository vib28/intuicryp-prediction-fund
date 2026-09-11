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

`--dry-run` computes the whole pipeline and WRITES NOTHING: no burn applied, no
settlement booked, no predictions recorded, no summary persisted. A dry run that
mutates the live book is not a dry run, it is a trade.
"""

import json
import os
import sys

import auditor
import calibration
import config
import forecaster
import ledger
import risk
import scout
import smile
import venue

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_CYCLE = os.path.join(HERE, "state", "last_cycle.json")
REJECTIONS = os.path.join(HERE, "state", "rejections.jsonl")


def settle_resolved() -> list[dict]:
    """Ask the real oracle which open positions have resolved.

    Only acts when the market is closed AND its outcome prices are decisive —
    `venue.resolution_outcome` owns that rule so settlement and calibration
    cannot drift apart on it.
    """
    settled = []
    for pos in ledger.open_positions():
        try:
            market = venue.settled_market(pos["market_id"])
        except Exception as exc:                      # noqa: BLE001
            settled.append({"id": pos["id"], "error": str(exc)[:80]})
            continue

        won = venue.resolution_outcome(market)
        if won is None:                               # closed but not decisive
            continue

        evidence = {
            "closed": True,
            "outcomePrices": market.get("outcomePrices"),
            "umaResolutionStatuses": market.get("umaResolutionStatuses"),
            "settled_market_slug": market.get("slug"),
        }
        settled.append(ledger.settle_position(pos, won, evidence))
    return settled


def _write_rejections(ts: str, cfg: dict, scout_rejects, forecast_rejects,
                      risk_rejects) -> None:
    """Append the rejection ledger so 'no edge found' stays falsifiable.

    Capped, because ~1,500 rejections per cycle would otherwise grow without
    bound; one generation is rotated out and kept.
    """
    os.makedirs(os.path.dirname(REJECTIONS), exist_ok=True)
    cap = cfg["scan"].get("rejections_max_bytes", 5_000_000)
    if os.path.exists(REJECTIONS) and os.path.getsize(REJECTIONS) > cap:
        os.replace(REJECTIONS, REJECTIONS + ".1")
    with open(REJECTIONS, "a") as fh:
        for r in scout_rejects:
            fh.write(json.dumps({"ts": ts, "stage": "scout", **r}) + "\n")
        for r in forecast_rejects:
            fh.write(json.dumps({"ts": ts, "stage": "forecast", **r}) + "\n")
        for r in risk_rejects:
            fh.write(json.dumps({"ts": ts, "stage": "risk",
                                 "slug": r["slug"], "reasons": r["reasons"]}) + "\n")


def run(dry_run: bool = False) -> dict:
    cfg = config.load()
    ledger.ensure_state()

    burned = ledger.accrue_burn(apply=not dry_run, cfg=cfg)
    settled = [] if dry_run else settle_resolved()

    candidates, scout_rejects = scout.scan(cfg)
    priced, forecast_rejects = forecaster.forecast_all(candidates)
    anchored, anchor_skips = smile.anchor(priced)
    approved, risk_rejects = risk.evaluate(anchored, cfg)
    booked = risk.execute(approved, cfg, dry_run=dry_run)

    # Calibration runs on EVERY priced market, traded or not. The fund may take
    # no trades for weeks; that must not also mean no evidence about the model.
    recorded = 0 if dry_run else calibration.record(anchored)
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
            {"slug": a["slug"],
             "ev_on_stake_pct": round(a["edge"]["ev_on_stake_pct"], 2),
             "edge_prob": round(a["edge"]["edge_prob"], 4),
             "p_trade": round(a["forecast"]["p_trade"], 4),
             "sigma_market": _round(smile.sigma_market_of(a["forecast"])),
             "sigma_realized": _round(a["forecast"].get("sigma_realized")),
             "cost_per_share": round(a["edge"]["cost_per_share"], 4)}
            for a in approved[:5]
        ],
        "survival_mode": risk.survival_mode(totals["equity_usd"], cfg),
        "totals": totals,
        "audit": {"passed": audit["passed"], "checks": audit["checks"],
                  "findings": len(audit["findings"]),
                  "warnings": len(audit.get("warnings") or [])},
        "dry_run": dry_run,
    }

    if dry_run:
        return summary

    os.makedirs(os.path.dirname(LAST_CYCLE), exist_ok=True)
    with open(LAST_CYCLE, "w") as fh:
        json.dump(summary, fh, indent=2)
    _write_rejections(summary["ts"], cfg, scout_rejects, forecast_rejects, risk_rejects)
    return summary


def _round(value, places: int = 4):
    return round(float(value), places) if value is not None else None


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = run(dry_run=dry)
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["audit"]["passed"] else 1)
