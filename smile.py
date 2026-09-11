"""
Smile anchoring — the guard that stops this fund trading a vol view it cannot
defend.

THE PROBLEM
-----------
A naive "price it with realized vol and compare to the market" model produces a
fake edge on nearly every market, because realized vol and market-implied vol
usually disagree. Measured on the first live run:

    bitcoin-above-78k-on-september-12-2026
      market ask           0.150
      realized-vol model   0.253   (sigma 31.1%)
      ~> implied vol of the market's own price: ~20%

The "+0.05 probability edge" was nothing but "our trailing vol is higher than
the market's forward vol". That is a volatility VIEW, not a mispricing, and a
$100 account punting a vol view it cannot evidence will be destroyed slowly.

THE FIX
-------
Don't use our vol. Use the market's OWN vol, recovered from its siblings.

Within one event (same underlying, same expiry) there are ~11 strikes. Fit ONE
sigma that best explains the whole strike ladder (least squares over log-sigma),
then re-price every strike with it. An edge now means exactly one thing:

    this strike is mispriced relative to the vol curve its own siblings imply

That is a real, testable dislocation rather than a vol opinion. Events with too
few strikes to establish a consensus are skipped.

Why a least-squares fit rather than the median of per-strike implied vols:
saturation. A strike priced 0.998 barely moves as sigma changes, so its squared
error is nearly flat and it contributes almost no gradient — it down-weights
itself. A near-the-money strike is highly sensitive and dominates the fit, which
is exactly right. A median treats a saturated strike as equally informative and
biases the consensus toward the extremes. The naive median is still computed,
stored as `sigma_market_raw_median`, and used as the fallback if the fit fails,
so any disagreement between the two stays visible in the audit trail.

The realized-vol estimate is likewise kept as context (`sigma_realized`).
"""

import math

import forecaster

MIN_STRIKES_FOR_CONSENSUS = 4
IV_LO, IV_HI = 0.05, 5.0
IV_TOL = 1e-4


def implied_vol(price: float, spot: float, strike: float, years: float,
                family: str) -> float | None:
    """Bisect for the sigma that reproduces `price`. None if not bracketed."""
    if not (0.0 < price < 1.0) or years <= 0 or spot <= 0 or strike <= 0:
        return None

    def model(sigma: float) -> float:
        return forecaster._prob(family, spot, strike, sigma, years)

    lo, hi = IV_LO, IV_HI
    p_lo, p_hi = model(lo), model(hi)
    if math.isnan(p_lo) or math.isnan(p_hi):
        return None

    # Prices are monotone in sigma for these families; if the target sits
    # outside the bracket, the price is not explainable by vol alone.
    if not (min(p_lo, p_hi) - IV_TOL <= price <= max(p_lo, p_hi) + IV_TOL):
        return None

    rising = p_hi > p_lo
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p_mid = model(mid)
        if math.isnan(p_mid):
            return None
        if abs(p_mid - price) < IV_TOL:
            return mid
        if (p_mid < price) == rising:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    return 0.5 * (lo + hi)


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def fit_sigma(strikes_asks: list[tuple[float, float]], spot: float,
              years: float, family: str) -> float | None:
    """Least-squares sigma across a whole strike ladder.

    Better than taking the median of per-strike implied vols, and the reason is
    saturation: a strike priced 0.998 barely moves as sigma changes, so its
    squared error is nearly flat and it contributes almost no gradient — it
    down-weights itself. A near-the-money strike is highly sensitive and
    dominates the fit, which is exactly right. A median treats a saturated
    strike as equally informative and biases the consensus toward the extremes.
    """
    if len(strikes_asks) < 2 or years <= 0 or spot <= 0:
        return None

    def sse(sigma: float) -> float:
        tot = 0.0
        for strike, ask in strikes_asks:
            p = forecaster._prob(family, spot, strike, sigma, years)
            if math.isnan(p):
                return float("inf")
            tot += (p - ask) ** 2
        return tot

    lo, hi = math.log(IV_LO), math.log(IV_HI)
    best = None
    for _ in range(300):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        s1, s2 = sse(math.exp(m1)), sse(math.exp(m2))
        if s1 < s2:
            hi = m2
            best = math.exp(m1)
        else:
            lo = m1
            best = math.exp(m2)
        if hi - lo < 1e-7:
            break
    return best if best and math.isfinite(best) else None


def _group_key(c: dict) -> tuple:
    """Siblings share underlying + expiry. End date is truncated to the day so
    near-identical timestamps from the same event still group."""
    return (c["symbol"], (c.get("end_date") or "")[:10], c["family"].split("_")[0])


def _median_of_implied_vols(members: list[dict]) -> float | None:
    """Fallback consensus: median of each strike's own implied vol.

    Costly (one bisection per strike), so it is only evaluated when the ladder
    fit fails — see `anchor`.
    """
    ivs = []
    for c in members:
        f = c["forecast"]
        iv = implied_vol(float(c["best_ask"]), f["spot"], f["strike"],
                         f["years"], f["family"])
        if iv is not None:
            ivs.append(iv)
    return _median(ivs) if ivs else None


def sigma_market_of(forecast: dict) -> float | None:
    """The consensus sigma a forecast was anchored to.

    Reads the current key and falls back to its pre-rename name, because
    predictions recorded before the rename are still in the calibration file and
    are still evidence.
    """
    value = forecast.get("sigma_market")
    if value is None:
        value = forecast.get("sigma_market_median")
    return float(value) if value is not None else None


def anchor(priced: list[dict]) -> tuple[list[dict], list[dict]]:
    """Attach market-consensus vol and the strike's deviation from it."""
    groups: dict[tuple, list[dict]] = {}
    for c in priced:
        groups.setdefault(_group_key(c), []).append(c)

    anchored, skipped = [], []
    for key, members in groups.items():
        # Build the ladder from EVERY strike in the event, including the
        # near-certain ones the fund may not trade: they still inform the curve.
        ladder = [(c["forecast"]["strike"], float(c["best_ask"])) for c in members]
        if len(ladder) < MIN_STRIKES_FOR_CONSENSUS:
            skipped.append({"group": list(key), "size": len(members),
                            "ladder": len(ladder), "reason": "no_vol_consensus"})
            continue

        ref = members[0]["forecast"]
        sigma_fit = fit_sigma(ladder, ref["spot"], ref["years"], ref["family"])
        sigma_median = None if sigma_fit is not None else _median_of_implied_vols(members)
        sigma_mkt = sigma_fit if sigma_fit is not None else sigma_median
        if sigma_mkt is None:
            skipped.append({"group": list(key), "size": len(members),
                            "reason": "vol_fit_failed"})
            continue

        for c in members:
            f = dict(c["forecast"])
            p_anchor = forecaster._prob(f["family"], f["spot"], f["strike"],
                                        sigma_mkt, f["years"])
            p_anchor_low = forecaster._prob(f["family"], f["spot"], f["strike"],
                                            sigma_mkt * forecaster.VOL_LOW_MULT, f["years"])
            p_anchor_high = forecaster._prob(f["family"], f["spot"], f["strike"],
                                             sigma_mkt * forecaster.VOL_HIGH_MULT, f["years"])
            finite = [v for v in (p_anchor, p_anchor_low, p_anchor_high)
                      if not math.isnan(v)]
            p_cons = min(finite) if finite else p_anchor

            f.update({
                # `sigma_market` is the consensus the fund actually prices with.
                # It is a least-squares fit, which is why it is no longer called
                # `sigma_market_median` — the old name described a method this
                # module had already replaced, which made the audit trail lie.
                "sigma_market": sigma_mkt,
                "sigma_market_raw_median": sigma_median,
                "sigma_method": ("least_squares_ladder_fit" if sigma_fit is not None
                                 else "median_of_implied_vols"),
                "ladder_size": len(ladder),
                "sigma_realized": f.get("sigma_annual"),
                "vol_disagreement_pct": 100.0 * (f.get("sigma_annual", 0) - sigma_mkt)
                                        / sigma_mkt if sigma_mkt else 0.0,
                "p_anchored": p_anchor,
                "p_anchored_conservative": p_cons,
                # the number the fund actually trades on: anchored, worst-case vol
                "p_trade": p_cons,
                "iv_source": ("least-squares sigma fit across the event's strike "
                              "ladder (same underlying + expiry)"),
            })
            anchored.append({**c, "forecast": f})

    anchored.sort(key=lambda c: -(c["forecast"]["p_trade"] - float(c["best_ask"])))
    return anchored, skipped
