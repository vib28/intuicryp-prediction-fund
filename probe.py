"""
Probe — measure ONE dry pass: per-stage seconds and upstream call counts.

Written to run against both the pre-refactor and post-refactor trees so the
effect of the caching change is measured rather than asserted.
"""

import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import forecaster
import ledger
import risk
import scout
import smile
import venue

calls = collections.Counter()
_real = venue._get_json


def _kind(url: str) -> str:
    if "binance" in url:
        if "interval=" in url:
            return "binance:klines"
        return "binance:ticker"
    if "/book" in url:
        return "clob:book"
    if "/events/slug/" in url:
        return "gamma:event"
    if "public-search" in url:
        return "gamma:search"
    if "/markets" in url:
        return "gamma:markets"
    return "gamma:other"


def spy(url, *args, **kwargs):
    calls[_kind(url)] += 1
    return _real(url, *args, **kwargs)


venue._get_json = spy

cfg = json.load(open(os.path.join(HERE, "config.json")))
ledger.ensure_state()

t0 = time.time()
candidates, _ = scout.scan(cfg)
t1 = time.time()
priced, _ = forecaster.forecast_all(candidates)
t2 = time.time()
anchored, _ = smile.anchor(priced)
t3 = time.time()
approved, _ = risk.evaluate(anchored, cfg)
t4 = time.time()

print(json.dumps({
    "candidates": len(candidates),
    "priced": len(priced),
    "anchored": len(anchored),
    "approved": len(approved),
    "seconds": {"scout": round(t1 - t0, 1), "forecaster": round(t2 - t1, 1),
                "smile": round(t3 - t2, 1), "risk": round(t4 - t3, 1),
                "total": round(t4 - t0, 1)},
    "upstream_calls": dict(calls),
    "upstream_total": sum(calls.values()),
}, indent=2))
