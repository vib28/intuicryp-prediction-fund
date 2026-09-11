"""
Data access — the only file that talks to the outside world.

Three upstreams, all read-only and keyless:
  * Polymarket Gamma  (gamma-api.polymarket.com) — market discovery/metadata
  * Polymarket CLOB   (clob.polymarket.com)      — real order books
  * Binance           (api.binance.com)          — spot price + realized vol

Nothing here can place an order: there is no key, no signer, and no write
endpoint. This fund is paper-only by construction, not by configuration.
"""

import json
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com"

# Crypto-related Gamma tag ids (verified live): crypto=21, bitcoin=235,
# ethereum=39. We scan the crypto tag and keep price-threshold markets.
CRYPTO_TAG = 21

_UA = {"User-Agent": "hermes-prediction-fund/1.0"}


def _get_json(url: str, timeout: int = 30, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as exc:                     # noqa: BLE001 - surface after retries
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# ---------------------------------------------------------------- Polymarket

def crypto_markets(limit_pages: int = 6, page_size: int = 100) -> list[dict]:
    """Open, tradeable markets from the crypto tag, paginated.

    Uses the keyset endpoint; the response key is `markets` (not `data`),
    and `next_cursor` drives pagination.
    """
    out, cursor, pages = [], None, 0
    while pages < limit_pages:
        q = {"tag_id": CRYPTO_TAG, "closed": "false", "limit": page_size}
        if cursor:
            q["after_cursor"] = cursor
        payload = _get_json(f"{GAMMA}/markets/keyset?" + urllib.parse.urlencode(q))
        batch = payload.get("markets") or []
        if not batch:
            break
        out.extend(batch)
        cursor = payload.get("next_cursor")
        pages += 1
        if not cursor:
            break
    return out


def market_tokens(market: dict) -> list[str]:
    """CLOB token ids for a market's outcomes (index 0 = 'Yes'/first outcome)."""
    raw = market.get("clobTokenIds")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw)


def order_book(token_id: str) -> dict:
    """Live CLOB book. NOTE: `bids` ascend and `asks` descend — never assume
    the best price is [0]; sort in costs.simulate_buy instead."""
    return _get_json(f"{CLOB}/book?" + urllib.parse.urlencode({"token_id": token_id}))


def best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = [float(b["price"]) for b in (book.get("bids") or []) if float(b["size"]) > 0]
    asks = [float(a["price"]) for a in (book.get("asks") or []) if float(a["size"]) > 0]
    return (max(bids) if bids else None, min(asks) if asks else None)


def settled_market(market_id: str) -> dict:
    """Re-fetch a market to read its resolution state.

    `outcomePrices` becomes ['1','0'] (or the reverse) once resolved; `closed`
    flips true. That is our settlement source — the real oracle, not our own
    assumption about how it should have gone.
    """
    return _get_json(f"{GAMMA}/markets/{market_id}")


def event_by_slug(slug: str) -> dict:
    """Fetch an event (with nested markets) by its slug.

    Needed because the high-volume daily price markets
    (`bitcoin-above-on-<date>`) are NOT returned by the crypto tag listing.
    """
    return _get_json(f"{GAMMA}/events/slug/{slug}")


def public_search(query: str, limit_per_type: int = 20) -> dict:
    """Gamma public search — how the monthly 'what price will X hit' families
    are discovered, since they are not reliably in the tag page order."""
    return _get_json(f"{GAMMA}/public-search?"
                     + urllib.parse.urlencode({"q": query,
                                               "limit_per_type": limit_per_type}))


# ------------------------------------------------------------------- Binance

def spot(symbol: str = "BTCUSDT") -> float:
    d = _get_json(f"{BINANCE}/api/v3/ticker/price?symbol={symbol}")
    return float(d["price"])


def realized_vol(symbol: str = "BTCUSDT", interval: str = "1h",
                 lookback: int = 168) -> dict:
    """Annualised realized volatility from log returns.

    `lookback` controls the window; 168 hourly bars = 7 days. We report the
    sample size so the forecaster can refuse to price on a thin sample.
    """
    kl = _get_json(
        f"{BINANCE}/api/v3/klines?"
        + urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": lookback})
    )
    closes = [float(k[4]) for k in kl]
    if len(closes) < 10:
        return {"ok": False, "reason": f"only {len(closes)} bars"}
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = var ** 0.5
    per_year = {"1m": 525600, "5m": 105120, "15m": 35040, "1h": 8760, "1d": 365}[interval]
    return {
        "ok": True,
        "report_vol": sd * (per_year ** 0.5),
        "abs_mean_move": sum(abs(r) for r in rets) / n,
        "bars": len(closes),
        "interval": interval,
        "samples": n,
    }
