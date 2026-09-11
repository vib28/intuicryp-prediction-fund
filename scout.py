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
"""

import re
from datetime import datetime, timedelta, timezone

import venue

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

# Families we can price. Order matters: check more specific patterns first.
_DIGITAL = re.compile(r"above-(\d+)(?:pt(\d+))?k", re.I)
_TOUCH_DN = re.compile(r"dip-to-(\d+)(?:pt(\d+))?k", re.I)
_TOUCH_DN_NUM = re.compile(r"dip-to-(\d{4,})(?![\dk])", re.I)
_TOUCH_UP = re.compile(r"reach-(\d+)(?:pt(\d+))?k", re.I)
_TOUCH_UP_NUM = re.compile(r"reach-(\d{4,})(?![\dk])", re.I)

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


# backwards-compatible alias used by earlier probes
parse_digital = parse_family


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


def scan(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Return (candidates, rejections)."""
    u = cfg["universe"]
    markets = discover(cfg)
    candidates, rejections = [], []

    # Cadence invariant, stated rather than magic-numbered: never hold a market
    # whose remaining life is shorter than a multiple of the scan interval, or
    # the fund is holding something it cannot observe between ticks.
    cadence_days = cfg["scan"]["interval_minutes"] / 1440.0
    multiple = cfg["scan"].get("horizon_cadence_multiple", 6)
    min_horizon = max(u["min_days_to_resolution"], cadence_days * multiple)

    for m in markets:
        slug = m.get("slug") or ""
        q = m.get("question") or ""
        vol24 = float(m.get("volume24hr") or 0)
        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        dtr = days_to(m.get("endDate"))

        def reject(reason, **extra):
            rejections.append({"slug": slug, "question": q[:90], "reason": reason,
                               "via": m.get("_discovered_via"), **extra})

        if str(m.get("closed")).lower() == "true" or str(m.get("active")).lower() != "true":
            reject("not_active"); continue
        if not m.get("enableOrderBook"):
            reject("no_orderbook"); continue
        if not m.get("acceptingOrders"):
            reject("not_accepting_orders"); continue
        if vol24 < u["min_volume_24h_usd"]:
            reject("volume_below_floor", vol24=round(vol24)); continue
        if dtr is None or dtr < min_horizon or dtr > u["max_days_to_resolution"]:
            reject("horizon_out_of_range", min_horizon_days=round(min_horizon, 4),
                   days=round(dtr, 2) if dtr is not None else None); continue

        tokens = venue.market_tokens(m)
        if len(tokens) < 2:
            reject("no_clob_tokens"); continue

        parsed = parse_family(slug)
        symbol = underlying_for(slug)
        if not parsed or not symbol:
            reject("not_priceable_family"); continue

        try:
            book = venue.order_book(tokens[0])
            bid, ask = venue.best_bid_ask(book)
        except Exception as exc:                       # noqa: BLE001
            reject("book_fetch_failed", error=str(exc)[:80]); continue
        if ask is None or bid is None:
            reject("no_two_sided_book"); continue
        spread = ask - bid
        if spread > u["max_spread"]:
            reject("spread_too_wide", spread=round(spread, 4)); continue
        # A near-certain market has no room to pay the fee: skip 1c/99c dead zones
        if ask <= u["min_ask"] or ask >= u["max_ask"]:
            reject("price_in_dead_zone", ask=ask); continue

        candidates.append({
            "market_id": str(m.get("id")),
            "slug": slug,
            "question": q,
            "symbol": symbol,
            "family": parsed["family"],
            "strike": parsed["strike"],
            "token_id_yes": tokens[0],
            "end_date": m.get("endDate"),
            "days_to_resolution": dtr,
            "volume24hr": vol24,
            "liquidity": liq,
            "best_bid": bid,
            "best_ask": ask,
            "spread": spread,
            "tick": m.get("orderPriceMinTickSize"),
            "fee_rate": (m.get("feeSchedule") or {}).get("rate"),
            "fee_type": m.get("feeType"),
            "fees_enabled": m.get("feesEnabled"),
            "discovered_via": m.get("_discovered_via"),
            "book_bids": book.get("bids"),
            "book_asks": book.get("asks"),
        })

    candidates.sort(key=lambda c: -c["volume24hr"])
    return candidates, rejections
