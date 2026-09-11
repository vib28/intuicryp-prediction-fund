"""
AGENT 2 — FORECASTER
Mandate: turn the underlying crypto reality into a fair probability, and be
explicit about how wrong it can be.

Two closed-form models, both driftless lognormal over horizon T (drift 0 is a
deliberate, conservative choice for short-horizon crypto — no momentum
assumption is smuggled in):

  digital_above  "Will X be above $K on <date>?"   terminal value only
        d2 = ( ln(S/K) - sigma^2/2 * T ) / ( sigma * sqrt(T) )
        P(above) = Phi(d2)

  touch_up       "Will X reach $K by <date>?"      one-touch barrier, K > S
        P(hit) = 2 * Phi( -ln(K/S) / (sigma*sqrt(T)) )        (reflection principle)

  touch_down     "Will X dip to $K by <date>?"     one-touch barrier, K < S
        P(hit) = 2 * Phi(  ln(K/S) / (sigma*sqrt(T)) )

sigma comes from Binance REALIZED volatility, not implied — that is the honest
weak point and it is labelled as such. Realized vol is backward-looking.

It is also NOT the vol the fund finally trades on. This module produces the
realized-vol view; smile.py then re-anchors every strike to the vol implied by
the event's own sibling strikes, and risk.py trades that. The two-stage split is
deliberate: an edge must survive BOTH "our vol says this is cheap" and "the
market's own vol curve agrees", otherwise it is just a vol opinion.

Because sigma is noisy we do not take the point estimate: we also price at
sigma*0.8 and sigma*1.2 and keep the WORST case for the direction we would
trade (buying YES). An edge that only exists at the point estimate is not an
edge, it is curve-fitting.

Anything outside these families is returned not_priceable and skipped. We do
not trade what we cannot value.
"""

import math

import venue

VOL_LOW_MULT = 0.8
VOL_HIGH_MULT = 1.2
MIN_VOL = 0.15
MAX_VOL = 5.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_digital_above(spot: float, strike: float, sigma: float, years: float) -> float:
    if min(spot, strike, sigma, years) <= 0:
        return float("nan")
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * years) / (sigma * math.sqrt(years))
    return norm_cdf(d2)


def prob_touch(family: str, spot: float, strike: float, sigma: float, years: float) -> float:
    """One-touch barrier probability for driftless GBM.

    Reflection principle: for driftless arithmetic Brownian motion in log
    space, P(max >= a) = 2 * P(X_T >= a). Applied per direction:

      touch_up   (reach K, K > S): already at/above -> 1, else 2*Phi(-ln(K/S)/(s*sqrt(T)))
      touch_down (dip to K, K < S): already at/below -> 1, else 2*Phi( ln(K/S)/(s*sqrt(T)))
    """
    if min(spot, strike, sigma, years) <= 0:
        return float("nan")

    if family == "touch_up":
        if strike <= spot:
            return 1.0                                  # barrier already breached
    elif family == "touch_down":
        if strike >= spot:
            return 1.0
    else:
        return float("nan")

    # Both directions then share one formula: the reflection principle gives
    # P(max |X| >= |a|) = 2 * Phi(-|a| / (sigma*sqrt(T))) in log space, so the
    # sign of the barrier distance cancels and only its magnitude matters.
    a = math.log(strike / spot)
    return min(1.0, 2.0 * norm_cdf(-abs(a) / (sigma * math.sqrt(years))))


def _prob(family: str, spot: float, strike: float, sigma: float, years: float) -> float:
    if family == "digital_above":
        return prob_digital_above(spot, strike, sigma, years)
    if family in ("touch_up", "touch_down"):
        return prob_touch(family, spot, strike, sigma, years)
    return float("nan")


def forecast(candidate: dict) -> dict:
    symbol = candidate["symbol"]
    strike = float(candidate["strike"])
    family = candidate["family"]
    years = max(0.0, float(candidate["days_to_resolution"] or 0)) / 365.0

    try:
        spot = venue.spot(symbol)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "reason": f"spot_unavailable: {str(exc)[:60]}"}

    # Horizon-matched vol: interval AND lookback are chosen from how long the
    # market has left to live, and recent returns are weighted more heavily
    # (EWMA) because vol clusters. venue.realized_vol memoizes per cycle, so the
    # ~80 markets collapse to a handful of Binance calls.
    interval, bars = venue.vol_plan_for_horizon(years * 365.0)
    halflife = max(2.0, bars / 3.0)

    try:
        vol = venue.realized_vol(symbol, interval, bars, ewma_halflife=halflife)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "reason": f"vol_unavailable: {str(exc)[:60]}"}

    if not vol.get("ok"):
        return {"ok": False, "reason": f"vol_{vol.get('reason')}"}
    sigma = float(vol["report_vol"])
    if not (MIN_VOL <= sigma <= MAX_VOL):
        return {"ok": False, "reason": f"sigma_out_of_band:{sigma:.2f}"}
    if years <= 0:
        return {"ok": False, "reason": "already_expired"}

    p_mid = _prob(family, spot, strike, sigma, years)
    if math.isnan(p_mid) or not (0.0 <= p_mid <= 1.0):
        return {"ok": False, "reason": "model_returned_nan"}

    p_low = _prob(family, spot, strike, sigma * VOL_LOW_MULT, years)
    p_high = _prob(family, spot, strike, sigma * VOL_HIGH_MULT, years)

    finite = [v for v in (p_low, p_mid, p_high) if not math.isnan(v)]
    p_conservative = min(finite) if finite else p_mid       # we buy YES -> worst case

    return {
        "ok": True,
        "family": family,
        "symbol": symbol,
        "spot": spot,
        "strike": strike,
        "years": years,
        "days": years * 365.0,
        "sigma_annual": sigma,
        "vol_interval": interval,
        "vol_bars": bars,
        "vol_weighting": vol.get("weighting"),
        "realized_bars": vol["bars"],
        "realized_samples": vol["samples"],
        "p_mid": p_mid,
        "p_low_vol": p_low,
        "p_high_vol": p_high,
        "p_conservative": p_conservative,
        "moneyness_pct": 100.0 * (spot - strike) / spot,
        "method": (f"{family}: driftless lognormal, "
                   f"{vol.get('weighting')} realized vol (Binance {interval} x {bars})"),
        "model_risk": "realized vol is backward-looking; sigma band is a crude error proxy",
    }


def forecast_all(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    priced, rejected = [], []
    for c in candidates:
        f = forecast(c)
        if f.get("ok"):
            priced.append({**c, "forecast": f})
        else:
            rejected.append({"slug": c["slug"], "reason": f.get("reason", "unknown")})
    return priced, rejected
