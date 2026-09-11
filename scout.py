"""
AGENT 1 — SCOUT
Mandate: find the tradeable universe and reject everything else.

The Scout does NOT have opinions about probability. It answers one question:
"is this market liquid enough, tight enough, and clear enough that a $100
account could trade it without being eaten alive?"

Discovery is EXPLICIT, not a tag dump. The high-volume price markets
(`bitcoin-above-on-<date>`, 11 strikes, ~$2M/day) do NOT appear in the crypto
tag listing at all — that listing is dominated by token-launch and regulation
markets. Three paths are merged and de-duplicated by market id:
    A. daily "above" events, addressed by slug for the next few days
    B. public search for the monthly "what price will X hit" families
    C. the crypto tag keyset scan, as a catch-all

Rejection reasons are recorded, not discarded — the audit needs to know what
was skipped and why, otherwise 'we found no edge' is unfalsifiable.

Scanning is TWO PHASES, and the split matters: every structural filter runs
first with no network access at all, and only the survivors get their order
book fetched — concurrently. Fetching books one at a time inside the filter
loop made the fetch the slowest part of every cycle (~95 sequential CLOB
round-trips, ~24s), and it fetched books for markets that were about to be
rejected on a field we already had in hand.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import costs
import venue

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

# Families we can price. Order matters: check more specific patterns first.
_DIGITAL = re.compile(r"above-(\d+)(?:pt(\d+))?k", re.I)
_TOUCH_DN = re.compile(r"dip-to-(\d+)(?:pt(\d+))?k", re.I)
_TOUCH_DN_NUM = re.compile(r"dip-to-(\d{4,})(?![\\dk])", re.I)
_TOUCH_UP = re.compile(r"reach-(\d+)(?:pt(\d+))?k", re.I)
_TOUCH_UP_NUM = re.compile(r"reach-(\d{4,})(?![\\dk])", re.I)

_UNDERLYING = [("bitcoin", "BTCUSDT"), ("btc", "BTCUSDT"),
               ("ethereum", "ETHUSDT"), ("eth", "ETHUSDT"),
               ("solana", "SOLUSDT"), ("sol", "SOLUSDT")]


def underlying_for(slug: str) -> str | None:
    s = (slug or "").lower()
    # 'eth' must not match inside 'ethereum' before we check it — longest first
    for key, symbol in sorted(_UNDERLYING, key=lambda kv: -len(kv[0])):
        if key in s:
            return symbol
    return None


def _value(whole: str, frac: str | None) -> float:
    """'70k' -> 70000, '97pt5k' -> 97500, '95000' -> 95000."""
    base = float(whole)
    if frac:
        base += float(frac) / (10 ** len(frac))
    return base * 1000.0 if (frac is not None or len(whole) <= 3) else base


def parse_family(slug: str) -> dict | None:
    """Classify a slug into a priceable family with its strike."""
    s = slug or ""
    m = _DIGITAL.search(s)
    if m:
        return {"family": "digital_above", "strike": _value(m.group(1), m.group(2))}
    m = _TOUCH_DN.search(s)
    if m:
        return {"family": "touch_down", "strike": _value(m.group(1), m.group(2))}
    m = _TOUCH_DN_NUM.search(s)
    if m:
        return {"family": "touch_down", "strike": float(m.group(1))}
    m = _TOUCH_UP.search(s)
    if m:
        return {"family": "touch_up", "strike": _value(m.group(1), m.group(2))}
    m = _TOUCH_UP_NUM.search(s)
    if m:
        return {"family": "touch_up", "strike": float(m.group(1))}
    return None


def days_to(end_iso: str | None) -> float | None:
    if not end_iso:
        return None
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 86400.0


def _date_slug(offset_days: int) -> str:
    d = datetime.now(timezone.utc) + timedelta(days=offset_days)
    return f"{MONTHS[d.month - 1]}-{d.day}-{d.year}"


def discover(cfg: dict) -> list[dict]:
    """Merge the three discovery paths into a unique market list."""
    found: dict[str, dict] = {}

    # A. daily "above" events
    for asset in cfg["scan"]["daily_assets"]:
        for off in range(1, cfg["scan"]["daily_days_ahead"] + 1):
            slug = f"{asset}-above-on-{_date_slug(off)}"
            try:
                ev = venue.event_by_slug(slug)
            except Exception:                          # noqa: BLE001
                continue
            for m in ev.get("markets") or []:
                m["_discovered_via"] = f"event:{slug}"
                found.setdefault(str(m.get("id")), m)

    # B. public search for the monthly price-target families
    for q in cfg["scan"]["search_queries"]:
        try:
            res = venue.public_search(q)
        except Exception:                              # noqa: BLE001
            continue
        for ev in res.get("events") or []:
            for m in ev.get("markets") or []:
                m["_discovered_via"] = f"search:{q}"
                found.setdefault(str(m.get("id")), m)

    # C. crypto tag catch-all
    for m in venue.crypto_markets(limit_pages=cfg["scan"]["pages"]):
        m.setdefault("_discovered_via", "tag:21")
        found.setdefault(str(m.get("id")), m)

    return list(found.values())


def _reject(rec: dict, reason: str, **extra) -> dict:
    return {"slug": rec["slug"], "question": rec["question"],
            "via": rec["discovered_via"], "reason": reason, **extra}


def _screen_market(m: dict, u: dict, min_horizon: float) -> tuple[dict | None, dict | None]:
    """Every filter that needs no network call.

    Returns (record, None) to proceed to the book fetch, or (None, rejection).
    """
    slug = m.get("slug") or ""
    rec = {"slug": slug, "question": (m.get("question") or "")[:90],
           "discovered_via": m.get("_discovered_via")}
    vol24 = float(m.get("volume24hr") or 0)
    liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
    dtr = days_to(m.get("endDate"))

    if str(m.get("closed")).lower() == "true" or str(m.get("active")).lower() != "true":
        return None, _reject(rec, "not_active")
    if not m.get("enableOrderBook"):
        return None, _reject(rec, "no_orderbook")
    if not m.get("acceptingOrders"):
        return None, _reject(rec, "not_accepting_orders")
    if vol24 < u["min_volume_24h_usd"]:
        return None, _reject(rec, "volume_below_floor", vol24=round(vol24))
    # Depth floor. This was configured from the start but never actually
    # applied — the check simply was not here, so `min_liquidity_usd` was a
    # number nobody read. A $100 account that cannot see $1k of resting
    # liquidity is trading a book it cannot get out of at a price it likes, and
    # settlement is the only exit this fund has.
    if liq < u["min_liquidity_usd"]:
        return None, _reject(rec, "liquidity_below_floor", liquidity=round(liq))
    if dtr is None or dtr < min_horizon or dtr > u["max_days_to_resolution"]:
        return None, _reject(rec, "horizon_out_of_range",
                             min_horizon_days=round(min_horizon, 4),
                             days=round(dtr, 2) if dtr is not None else None)

    tokens = venue.market_tokens(m)
    if len(tokens) < 2:
        return None, _reject(rec, "no_clob_tokens")

    parsed = parse_family(slug)
    symbol = underlying_for(slug)
    if not parsed or not symbol:
        return None, _reject(rec, "not_priceable_family")

    rec.update({
        "market_id": str(m.get("id")),
        "symbol": symbol,
        "family": parsed["family"],
        "strike": parsed["strike"],
        "token_id_yes": tokens[0],
        "end_date": m.get("endDate"),
        "days_to_resolution": dtr,
        "volume24hr": vol24,
        "liquidity": liq,
        "tick": m.get("orderPriceMinTickSize"),
        # The authoritative taker rate comes from the market payload via
        # costs.fee_rate_for_market. Reading `feeSchedule.rate` inline duplicated
        # that logic and dropped its feesEnabled fallback, so a market with fees
        # enabled but no schedule would have been read as 0%.
        "fee_rate": costs.fee_rate_for_market(m),
        "fee_type": m.get("feeType"),
        "fees_enabled": m.get("feesEnabled"),
    })
    return rec, None


def _fetch_books(token_ids: list[str], workers: int) -> dict:
    """Fetch many order books concurrently. Read-only, so safe to parallelise."""
    out: dict = {}
    if not token_ids:
        return out
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(venue.order_book, t): t for t in token_ids}
        for future in as_completed(futures):
            token = futures[future]
            try:
                out[token] = future.result()
            except Exception as exc:                   # noqa: BLE001
                out[token] = exc
    return out


def scan(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Return (candidates, rejections)."""
    u = cfg["universe"]
    candidates, rejections = [], []

    # Cadence invariant, stated rather than magic-numbered: never hold a market
    # whose remaining life is shorter than a multiple of the scan interval, or
    # the fund is holding something it cannot observe between ticks.
    cadence_days = cfg["scan"]["interval_minutes"] / 1440.0
    min_horizon = max(u["min_days_to_resolution"],
                      cadence_days * cfg["scan"]["horizon_cadence_multiple"])

    # Phase 1: everything decidable without a network call.
    pending = []
    for m in discover(cfg):
        rec, rejection = _screen_market(m, u, min_horizon)
        if rejection is not None:
            rejections.append(rejection)
        else:
            pending.append(rec)

    # Phase 2: fetch the survivors' books concurrently, then apply the
    # book-dependent filters.
    books = _fetch_books([r["token_id_yes"] for r in pending],
                         cfg["scan"].get("book_fetch_workers", 8))

    for rec in pending:
        book = books.get(rec["token_id_yes"])
        if isinstance(book, Exception):
            rejections.append(_reject(rec, "book_fetch_failed", error=str(book)[:80]))
            continue
        if not book:
            rejections.append(_reject(rec, "book_fetch_failed", error="empty book"))
            continue

        bid, ask = venue.best_bid_ask(book)
        if ask is None or bid is None:
            rejections.append(_reject(rec, "no_two_sided_book"))
            continue
        spread = ask - bid
        if spread > u["max_spread"]:
            rejections.append(_reject(rec, "spread_too_wide", spread=round(spread, 4)))
            continue

        # A near-certain strike has no room left to pay the fee, so we must not
        # TRADE it. But it still carries information about the market's implied
        # vol, and smile.py needs a full strike ladder to establish a consensus.
        # So it stays a candidate, flagged, and risk.py declines to trade it.
        #
        # Getting this wrong silently destroyed the entire daily universe: BTC
        # strikes are $2000 apart with ~1.6% daily vol, so only ~2 strikes sit
        # inside a 0.02-0.97 price band. Dropping the rest left groups of 2,
        # below the 4-strike minimum for a vol consensus, and every daily market
        # was discarded by the anchoring step.
        candidates.append({
            **rec,
            "best_bid": bid,
            "best_ask": ask,
            "spread": spread,
            "dead_zone": ask <= u["min_ask"] or ask >= u["max_ask"],
            "book_bids": book.get("bids"),
            "book_asks": book.get("asks"),
        })

    candidates.sort(key=lambda c: -c["volume24hr"])
    return candidates, rejections
