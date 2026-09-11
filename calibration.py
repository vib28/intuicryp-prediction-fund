"""
Calibration — is the model actually any good?

WHY THIS EXISTS
---------------
The fund takes few trades, because most cycles find no dislocation that clears
the cost hurdle. That means P&L is silent about whether the model works, and
"we found no edge" is indistinguishable from "the model is broken".

We do not need to risk money to find out. Every cycle prices dozens of markets,
and every market resolves. So we record each prediction AND grade it at
settlement, whether or not we traded it.

THE CLUSTERING TRAP (read this before trusting any number here)
--------------------------------------------------------------
A market living 3 days gets recorded once per 12h bucket, so it can contribute
6 graded rows with the SAME outcome. That inflates n and correlates the
observations, which makes any confidence in the result look far better than it
is. So this module reports TWO samples:

  * ALL PREDICTIONS — every graded row. More data, correlated.
  * ONE PER MARKET  — a single row per distinct market (the LAST forecast made
    before settlement, i.e. the most informed one). Independent, honest, and
    the number that should drive decisions.

If the two disagree materially, trust ONE PER MARKET and treat the headline
sample size as `distinct_markets`, not `rows`.

A second, subtler bias: we only record markets that already passed the Scout's
liquidity/horizon/spread filter. So this measures the model on LIQUID,
LONGER-DATED markets — not a random sample of crypto outcomes. It answers "is
the model honest where we would actually trade?", which is the useful question,
but it is not a general-purpose vol-model scorecard.

Metrics:
  * Brier score  — mean squared error of the probabilities. Lower is better.
                   0.25 is what you get by always saying 0.50.
  * Skill score  — 1 - (Brier / baseline). Positive = better than a coin flip.
  * Reliability  — bucketed predicted vs realized frequency. THE number that
                   matters: it exposes systematic over/under-confidence, and
                   the tails are exactly where this fund trades.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import ledger
import smile
import venue

PREDICTIONS = os.path.join(ledger.STATE_DIR, "predictions.jsonl")
BUCKET_HOURS = 12
MAX_BYTES = 5_000_000

# Reporting thresholds, deliberately not trading policy: below
# MIN_EVIDENCE_MARKETS any skill number is noise, and MIN_USEFUL_SKILL is the
# point below which an edge cannot pay a 3-9% round-trip cost hurdle.
MIN_EVIDENCE_MARKETS = 30
MIN_USEFUL_SKILL = 0.05


def _bucket(ts: float | None = None) -> str:
    ts = ts or time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt:%Y-%m-%d}T{(dt.hour // BUCKET_HOURS) * BUCKET_HOURS:02d}"


def record(anchored: list[dict]) -> int:
    """Append this cycle's predictions, deduped by (market, 12h bucket)."""
    os.makedirs(ledger.STATE_DIR, exist_ok=True)
    existing = set()
    if os.path.exists(PREDICTIONS):
        with open(PREDICTIONS) as fh:
            for line in fh:
                try:
                    existing.add(json.loads(line).get("key"))
                except json.JSONDecodeError:
                    continue

    bucket = _bucket()
    rows, added = [], 0
    for c in anchored:
        f = c.get("forecast") or {}
        p = f.get("p_trade", f.get("p_conservative"))
        if p is None:
            continue
        key = f"{c['market_id']}@{bucket}"
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
            "p_trade": p,
            "p_mid": f.get("p_mid"),
            "sigma_market": smile.sigma_market_of(f),
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
    by_key, graded = {}, 0
    for r in todo:
        try:
            market = venue.settled_market(r["market_id"])
        except Exception:                              # noqa: BLE001
            continue
        won = venue.resolution_outcome(market)
        if won is None:
            continue                                   # not resolved, or not decisive
        r["scored"] = True
        r["outcome"] = 1 if won else 0
        r["scored_ts"] = ledger.now_iso()
        by_key[r["key"]] = r
        graded += 1

    if graded:
        with open(PREDICTIONS, "w") as fh:
            for r in [by_key.get(x["key"], x) for x in rows]:
                fh.write(json.dumps(r) + "\n")
    return {"graded": graded, "total": len(rows)}


BUCKETS = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5),
           (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    brier = sum((r["p_trade"] - r["outcome"]) ** 2 for r in rows) / n
    base = sum((0.5 - r["outcome"]) ** 2 for r in rows) / n
    rel = []
    for lo, hi in BUCKETS:
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


def _one_per_market(rows: list[dict]) -> list[dict]:
    """Latest forecast per distinct market — the independent sample."""
    latest: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("ts") or ""):
        latest[r["market_id"]] = r
    return list(latest.values())


def summary() -> dict:
    rows = _load()                                     # one read, not two
    graded = [r for r in rows if r.get("scored") and r.get("outcome") is not None]
    per_market = _one_per_market(graded)
    return {
        "all": _stats(graded),
        "one_per_market": _stats(per_market),
        "distinct_markets": len(per_market),
        "pending": sum(1 for r in rows if not r.get("scored")),
    }


def verdict(s: dict) -> str:
    """One line the LP can act on, deliberately blunt."""
    m = s["one_per_market"]
    if s["distinct_markets"] < MIN_EVIDENCE_MARKETS:
        return (f"NOT ENOUGH EVIDENCE — {s['distinct_markets']} distinct markets graded "
                f"({s['pending']} pending). Do not draw conclusions yet.")
    if m["skill"] <= 0:
        return (f"MODEL FAILS — skill {m['skill']:+.4f} is no better than a coin flip "
                f"over {m['n']} markets. Do not trade on p_trade until this is fixed.")
    if m["skill"] < MIN_USEFUL_SKILL:
        return (f"MARGINAL — skill {m['skill']:+.4f} over {m['n']} markets. Beats a coin "
                f"flip but not by enough to overcome a 3-9% cost hurdle.")
    return (f"MODEL HOLDS — skill {m['skill']:+.4f} over {m['n']} markets. "
            f"Check the reliability table for tail bias before trusting size.")


def render() -> str:
    s = summary()
    t = ledger.totals()
    by_cat = t["burn_by_category"]
    one_off = sum(float(e.get("amount_usd") or 0) for e in ledger.one_off_burns())
    out = [
        "CALIBRATION — is the model honest?",
        "=" * 72,
        # Carried here too, not just in table.py: a calibration report that hides
        # what the answer cost is the same fiction as a P&L that ignores the burn.
        f"  cost so far   : burn ${t['burn_accrued_usd']:.4f} "
        f"(${by_cat['tokens']:.4f} tokens + ${by_cat['vps']:.4f} VPS)"
        + (f"  incl. ${one_off:.4f} billed outright" if one_off else ""),
        f"  equity        : ${t['equity_usd']:.4f}",
        "",
    ]
    if s["distinct_markets"] == 0:
        out.append("  No graded predictions yet.")
        out.append(f"  Recorded, awaiting resolution: {s['pending']}")
        out.append("")
        out.append("  Meaning: nothing can be concluded. The recorder runs every")
        out.append("  30 min, so this fills up as daily markets settle.")
        return "\n".join(out)

    a, m = s["all"], s["one_per_market"]
    out.append(f"  distinct markets graded : {s['distinct_markets']}")
    out.append(f"  graded rows (correlated): {a['n']}")
    out.append(f"  still pending           : {s['pending']}")
    out.append("")
    out.append(f"  {'sample':<18} {'n':>5} {'Brier':>8} {'baseline':>9} {'skill':>8}")
    for label, st in (("all predictions", a), ("one per market", m)):
        out.append(f"  {label:<18} {st['n']:>5} {st['brier']:>8.4f} "
                   f"{st['baseline_brier']:>9.4f} {st['skill']:>+8.4f}")
    out.append("")
    out.append("  RELIABILITY (one per market) — where the model lies")
    out.append(f"  {'bucket':>10} {'n':>5} {'predicted':>10} {'realized':>9} {'gap':>8}")
    for r in m["reliability"]:
        gap = r["realized"] - r["predicted"]
        flag = "  overconfident" if gap < -0.08 else ("  underconfident" if gap > 0.08 else "")
        out.append(f"  {r['bucket']:>10} {r['n']:>5} {r['predicted']:>10.3f} "
                   f"{r['realized']:>9.3f} {gap:>+8.3f}{flag}")
    out.append("")
    out.append("  VERDICT")
    out.append("  " + verdict(s))
    out.append("=" * 72)
    return "\n".join(out)


if __name__ == "__main__":
    if "--score" in sys.argv:
        print(json.dumps(score(), indent=2))
    print(render())
