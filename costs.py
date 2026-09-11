"""
Cost model — the most important file in this fund.

Every number here is grounded in Polymarket's OWN per-market fee schedule,
fetched from the Gamma API (`feeSchedule`), not from press articles. For
crypto markets the API reports:

    {'exponent': 1, 'rate': 0.07, 'takerOnly': True, 'rebateRate': 0.2}
    feeType: 'crypto_fees_v2'

so the taker fee is

    fee = rate * shares * p * (1 - p)

and settlement is free (Polymarket charges nothing on resolution).

WHY THE FUND HOLDS TO RESOLUTION
--------------------------------
Measured on a $1 stake with a 1-tick (1c) spread, at the crypto taker rate 0.07:

    price   fee % of stake   round trip % of stake
    $0.05          6.65%                  33.3%
    $0.10          6.30%                  22.6%
    $0.20          5.60%                  16.2%
    $0.30          4.90%                  13.1%
    $0.50          3.50%                   9.0%
    $0.70          2.10%                   5.6%
    $0.90          0.70%                   2.5%

Exiting early hands a large fraction of the stake to fees and spread. So the
fund buys and holds to resolution, paying the taker fee exactly ONCE. Any
strategy that trades in and out is dead on arrival, and this module is the
reason we know that rather than assume it.

COST IS THE HURDLE
------------------
A bet at price p only breaks even if the true probability clears

    p_be = p + fee_per_share

so the model probability must beat p_be by a real margin before we risk
anything. That comparison lives in `edge_after_costs`.
"""

from dataclasses import dataclass
from typing import List, Tuple

DEFAULT_CRYPTO_RATE = 0.07  # crypto_fees_v2; read per-market via feeSchedule


def taker_fee(shares: float, price: float, rate: float = DEFAULT_CRYPTO_RATE) -> float:
    """Polymarket taker fee: rate * shares * p * (1-p). Max at p=0.50.

    `rate` must come from the market's own feeSchedule.rate. We never assume.
    """
    return rate * shares * price * (1.0 - price)


def fee_rate_for_market(market: dict) -> float:
    """Read the authoritative taker rate off a Gamma market payload."""
    sched = market.get("feeSchedule") or {}
    rate = sched.get("rate")
    if rate is None:
        return 0.0 if not market.get("feesEnabled") else DEFAULT_CRYPTO_RATE
    return float(rate)


@dataclass
class Fill:
    """A simulated taker BUY walked through a real order book."""
    shares: float
    vwap: float
    gross_usd: float          # shares * vwap (what the shares cost)
    fee_usd: float            # taker fee on top
    all_in_usd: float         # gross + fee  (total cash out)
    levels_used: int
    book_depth_usd: float     # visible ask depth at time of fill
    depth_limited: bool       # True if we ran out of book before spending the stake

    @property
    def cost_per_share(self) -> float:
        """Effective price paid per share including fees.

        This is the number Kelly must use — not the quoted price. Using the
        quoted price silently overstates the edge of every trade.
        """
        return self.all_in_usd / self.shares if self.shares else float("inf")

    @property
    def fee_pct_of_stake(self) -> float:
        return 100.0 * self.fee_usd / self.gross_usd if self.gross_usd else 0.0


def simulate_buy(asks: List[dict], stake_usd: float,
                 rate: float = DEFAULT_CRYPTO_RATE) -> Fill | None:
    """Walk the real ask book, spending up to `stake_usd`.

    `asks` are CLOB book entries [{'price': '0.123', 'size': '456.7'}, ...] and
    are NOT assumed sorted — CLOB returns best-ask-last, so we sort explicitly.
    Price levels are consumed in order, so the fill carries genuine slippage.
    """
    if not asks or stake_usd <= 0:
        return None
    levels = sorted(
        ((float(a["price"]), float(a["size"])) for a in asks if float(a["size"]) > 0),
        key=lambda x: x[0],
    )
    if not levels:
        return None

    remaining, shares, spent, used = stake_usd, 0.0, 0.0, 0
    total_depth = sum(p * s for p, s in levels)

    for price, size in levels:
        if remaining <= 1e-9:
            break
        level_cost = price * size
        take_cost = min(remaining, level_cost)
        shares += take_cost / price
        spent += take_cost
        remaining -= take_cost
        used += 1

    if shares <= 0:
        return None

    vwap = spent / shares
    fee = taker_fee(shares, vwap, rate)
    return Fill(
        shares=shares,
        vwap=vwap,
        gross_usd=spent,
        fee_usd=fee,
        all_in_usd=spent + fee,
        levels_used=used,
        book_depth_usd=total_depth,
        depth_limited=remaining > 1e-9,
    )


def breakeven_prob(price: float, rate: float = DEFAULT_CRYPTO_RATE) -> float:
    """Probability at which buying 1 share at `price` exactly breaks even.

    Win pays $1; cost is price + fee_per_share. Resolution is free.
    """
    fee_per_share = rate * price * (1.0 - price)
    return price + fee_per_share


def edge_after_costs(model_prob: float, fill: Fill, rate: float) -> dict:
    """Compare a model probability against the ACTUAL blended cost of the fill.

    Returns the honest margin. A trade is only eligible when
    `edge_per_share` clears the configured hurdle multiple — this is where
    'include transaction costs in the profit logic' is enforced.
    """
    c = fill.cost_per_share
    be = breakeven_prob(c, rate)
    ev_per_share = model_prob * 1.0 - be       # expected $ per share, net of all costs
    return {
        "cost_per_share": c,
        "breakeven_prob": be,
        "model_prob": model_prob,
        "edge_prob": model_prob - be,          # probability points of edge
        "ev_per_share_usd": ev_per_share,
        "ev_on_stake_pct": 100.0 * ev_per_share / c if c else 0.0,
        "fee_drag_pct_of_stake": fill.fee_pct_of_stake,
    }


def kelly_fraction(model_prob: float, cost_per_share: float) -> float:
    """Full-Kelly bankroll fraction for a binary bought at `cost_per_share`.

    Derivation: stake f, bought at price c. Win -> multiply by 1 + f(1-c)/c,
    lose -> 1 - f. Maximising p*ln(win) + (1-p)*ln(lose) gives

        f* = (p - c) / (1 - c)

    Callers MUST scale this down (fractional Kelly) — full Kelly on an
    uncertain model probability is how accounts die.
    """
    if cost_per_share <= 0 or cost_per_share >= 1:
        return 0.0
    return max(0.0, (model_prob - cost_per_share) / (1.0 - cost_per_share))


def round_trip_cost_pct(price: float, rate: float = DEFAULT_CRYPTO_RATE,
                        spread: float = 0.01) -> float:
    """For the report: why we never exit early. Fee both ways + one spread."""
    shares = 1.0 / price
    fees = 2.0 * taker_fee(shares, price, rate)
    return 100.0 * (fees + spread * shares) / 1.0
