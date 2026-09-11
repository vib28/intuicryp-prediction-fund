"""
Calibration — is the model actually any good?

WHY THIS EXISTS
---------------
The fund has taken ZERO trades, because no dislocation has cleared the cost
hurdle. That means we have no evidence at all about whether the model works,
and "we found no edge" is indistinguishable from "the model is broken".

We do not need to risk money to find out. Every cycle prices ~56 markets. Each
one resolves. So we record the prediction AND score it at settlement, whether or
not we traded it. Within a week that gives hundreds of graded predictions for
free, which is the only honest way to answer:

    when the model says 0.20, does it happen 20% of the time?

Metrics:
  * Brier score  — mean squared error of the probabilities. Lower is better.
                   0.25 is the score of always saying 0.50 (a coin flip).
  * Reliability  — bucketed predicted vs realized frequency. THIS is the number
                   that matters: it exposes systematic over/under-confidence.
                   A model can have a good Brier score and still be badly
                   miscalibrated in the tails, which is exactly where we trade.
  * Skill score  — 1 - (Brier / 0.25). Positive means better than a coin flip.

Predictions are recorded at most once per market per bucket window, so volume
stays bounded (56 markets x 2 buckets/day instead of 56 x 48 cycles).
"""

import json
import os
import time
from datetime import datetime, timezone

import ledger
import venue

PREDICTIONS = os.path.join(ledger.STATE_DIR, "predictions.jsonl")
BUCKET_HOURS = 12
MAX_BYTES = 5_000_000


def _bucket(ts: float | None = None) -> str:
    ts = ts or time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt:%Y-%m-%d}T{(dt.hour // BUCKET_HOURS) * BUCKET_HOURS:02d}"


def record(anchored: list[dict]) -> int:
    """Append this cycle's predictions, deduped by (market, 12h bucket)."""
    os.makedirs(ledger.STATE_DIR, exist_ok=True)
    seen = set()
    existing = {}
    if os.path.exists(PREDICTIONS):
        with open(PREDICTIONS) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                existing[e.get("key")] = e

    bucket = _bucket()
    rows, added = [], 0
    for c in anchored:
        f = c.get("forecast") or {}
        p = f.get("p_trade", f.get("p_conservative"))
        if p is None:
            continue
        key = f"{c['market_id']}@{bucket}"
        seen.add(key)
        if key in existing:
            continue
        rows.append({
            "key": key,
            "ts": ledger.now_iso(),
            "market_id": c["market_id"],
            "slug": c["slug"],
            "family": c.get("family"),
            "symbol": c.get("symbol"),
            "strike": c.get("strike"),
            "spot": f.get("spot"),
            "days_to_resolution": c.get("days_to_resolution"),
            "p_trade": p,                      # the number the fund would trade on
            "p_mid": f.get("p_mid"),
            "sigma_market": f.get("sigma_market_median"),
            "sigma_realized": f.get("sigma_realized"),
            "ask": c.get("best_ask"),
            "end_date": c.get("end_date"),
            "scored": False,
        })
        added += 1

    if rows:
        if os.path.exists(PREDICTIONS) and os.path.getsize(PREDICTIONS) > MAX_BYTES:
            os.replace(PREDICTIONS, PREDICTIONS + ".1")
        with open(PREDICTIONS, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    return added


def _load() -> list[dict]:
    if not os.path.exists(PREDICTIONS):
        return []
    out = []
    with open(PREDICTIONS) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def score(limit: int = 400) -> dict:
    """Grade every unresolved prediction whose market has now resolved."""
    rows = _load()
    todo = [r for r in rows if not r.get("scored")][:limit]
    graded = 0
    by_key = {}
    for r in todo:
        try:
            m = venue.settled_market(r["market_id"])
        except Exception:                              # noqa: BLE001
            continue
        if str(m.get("closed")).lower() != "true":
            continue
        raw = m.get("outcomePrices")
        try:
            prices = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        except (json.JSONDecodeError, TypeError):
            continue
        if not prices:
            continue
        yes = float(prices[0])
        if not (yes > 0.99 or yes < 0.01):
            continue                                   # closed, not decisive yet
        r["scored"] = True
        r["outcome"] = 1 if yes > 0.5 else 0
        r["scored_ts"] = ledger.now_iso()
        graded += 1
        by_key[r["key"]] = r

    if graded:
        merged = [by_key.get(r["key"], r) for r in rows]
        with open(PREDICTIONS, "w") as fh:
            for r in merged:
                fh.write(json.dumps(r) + "\n")
    return {"graded": graded, "total": len(rows)}


def summary() -> dict:
    """Brier score, skill score and a reliability table."""
    rows = [r for r in _load() if r.get("scored") and r.get("outcome") is not None]
    n = len(rows)
    if n == 0:
        return {"n": 0}
    brier = sum((r["p_trade"] - r["outcome"]) ** 2 for r in rows) / n
    base = sum((0.5 - r["outcome"]) ** 2 for r in rows) / n
    buckets = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5),
               (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    rel = []
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= r["p_trade"] < hi]
        if not sel:
            continue
        rel.append({
            "bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}",
            "n": len(sel),
            "predicted": sum(r["p_trade"] for r in sel) / len(sel),
            "realized": sum(r["outcome"] for r in sel) / len(sel),
        })
    return {
        "n": n,
        "brier": brier,
        "baseline_brier": base,
        "skill": 1.0 - (brier / base) if base else 0.0,
        "reliability": rel,
    }


def render() -> str:
    s = summary()
    out = ["CALIBRATION — is the model honest?", "=" * 68]
    if s["n"] == 0:
        out.append("  No graded predictions yet.")
        pending = len([r for r in _load() if not r.get("scored")])
        out.append(f"  Recorded, awaiting resolution: {pending}")
        return "\n".join(out)
    out.append(f"  graded predictions : {s['n']}")
    out.append(f"  Brier score        : {s['brier']:.4f}   (0.25 = always saying 0.50)")
    out.append(f"  skill vs coin flip : {s['skill']:+.4f}   "
               f"({'better' if s['skill'] > 0 else 'WORSE'} than a coin flip)")
    out.append("")
    out.append(f"  {'bucket':>10} {'n':>5} {'predicted':>10} {'realized':>9} {'gap':>8}")
    for r in s["reliability"]:
        gap = r["realized"] - r["predicted"]
        flag = "  <- overconfident" if gap < -0.08 else ("  <- underconfident" if gap > 0.08 else "")
        out.append(f"  {r['bucket']:>10} {r['n']:>5} {r['predicted']:>10.3f} "
                   f"{r['realized']:>9.3f} {gap:>+8.3f}{flag}")
    out.append("=" * 68)
    return "\n".join(out)


if __name__ == "__main__":
    if "--score" in os.sys.argv:
        print(json.dumps(score(), indent=2))
    print(render())
