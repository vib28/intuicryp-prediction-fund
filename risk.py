"""
AGENT 3 — RISK & EXECUTION
Mandate: decide HOW MUCH, fill against the real book, and refuse anything that
does not clear its costs.

This agent is the pessimist of the fund. Its default answer is "no".

Every threshold it applies comes from config.py. It used to hold its own copy of
the EV hurdle as a module constant, which meant `risk.min_ev_on_stake_pct` in
config.json was decorative — editing it changed nothing. A policy number that
does not reach the code applying it is worse than no config at all.
"""

import json
import os
import time

import config
import costs
import ledger


def snapshot_path(pos_hint: str) -> str:
    """Where the raw evidence for this fill is frozen for the Auditor."""
    os.makedirs(ledger.SNAPSHOTS, exist_ok=True)
    return os.path.join(ledger.SNAPSHOTS, f"{pos_hint}.json")


def trade_prob(forecast: dict) -> float:
    """The probability the fund actually trades on.

    `p_trade` is the smile-anchored, worst-case-vol probability. Falling back to
    `p_conservative` would mean trading our own vol opinion, which is exactly
    what smile.py exists to prevent — so the fallback is only for diagnostics,
    never silent.
    """
    if "p_trade" in forecast:
        return float(forecast["p_trade"])
    return float(forecast.get("p_conservative", 0.0))


def _rate(row: dict) -> float:
    return float(row.get("fee_rate") or costs.DEFAULT_CRYPTO_RATE)


def evaluate(priced: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Rank opportunities by expected value per dollar staked, net of costs."""
    equity = ledger.totals()["equity_usd"]
    probe_stake = min(float(cfg["risk"]["probe_stake_usd"]),
                      cfg["risk"]["max_position_pct"] / 100.0 * equity)
    if probe_stake <= 0:
        return [], []

    rows = []
    for c in priced:
        # Price the fill at the size we could actually afford, before sizing.
        fill = costs.simulate_buy(c["book_asks"], probe_stake, rate=_rate(c))
        if not fill:
            continue
        p = trade_prob(c["forecast"])
        rows.append({**c,
                     "probe_fill": fill,
                     "edge": costs.edge_after_costs(p, fill, _rate(c)),
                     "kelly_full": costs.kelly_fraction(p, fill.cost_per_share)})

    rows.sort(key=lambda r: -r["edge"]["ev_on_stake_pct"])

    hurdle = config.min_ev_on_stake_pct(cfg)
    approved, rejected = [], []
    for r in rows:
        reasons = []
        if r.get("dead_zone"):
            reasons.append(f"price_in_dead_zone:{r['best_ask']:.3f}")
        if r["edge"]["ev_on_stake_pct"] < hurdle:
            reasons.append(f"ev_below_hurdle:{r['edge']['ev_on_stake_pct']:.2f}%")
        if r["edge"]["edge_prob"] <= 0:
            reasons.append("no_probability_edge")
        if r["kelly_full"] <= 0:
            reasons.append("kelly_nonpositive")
        if (r["probe_fill"].depth_limited
                and r["probe_fill"].book_depth_usd < cfg["risk"]["min_book_depth_usd"]):
            reasons.append(f"book_too_thin:{r['probe_fill'].book_depth_usd:.0f}")
        (rejected if reasons else approved).append({**r, "reasons": reasons})
    return approved, rejected


def size(edge_row: dict, totals: dict, cfg: dict) -> float:
    """Position size in USD.

    TWO modes, and the choice is a policy decision, not a detail:

    * "kelly"           — fractional Kelly. Theoretically correct for a KNOWN
                          edge. On this book it sizes real-but-small edges at
                          ~$0.43, i.e. below any sensible minimum: the fund
                          would hold a mathematically perfect book of nothing
                          and never grow. Reported as a diagnostic always.
    * "fixed_fraction"  — a flat % of equity for anything that clears the EV
                          hurdle. Deliberately OVER-sizes relative to Kelly,
                          which is the only way a $100 account targeting 10x
                          can deploy capital at all. The cost of that choice is
                          a fatter left tail, which is why the position cap,
                          the concurrency cap and the survival ladder exist.

    Kelly is still computed and stored on every row so the over-betting is
    visible in the audit trail rather than hidden in a config flag.
    """
    r = cfg["risk"]
    equity = totals["equity_usd"]

    if r["sizing_mode"] == "kelly":
        raw = equity * edge_row["kelly_full"] * r["fractional_kelly"]
    else:
        raw = equity * r["fixed_fraction_pct"] / 100.0

    stake = min(max(raw, r["min_position_usd"]),
                equity * r["max_position_pct"] / 100.0,
                r["absolute_max_position_usd"],
                totals["cash_usd"] * 0.98)
    return max(0.0, stake)


def execute(approved: list[dict], cfg: dict, dry_run: bool = False) -> list[dict]:
    """Book paper positions, subject to concurrency and survival policy."""
    if dry_run:
        return []

    totals = ledger.totals()
    mode = survival_mode(totals["equity_usd"], cfg)
    if mode != "normal":
        return [{"skipped": True, "reason": f"survival_mode:{mode}"}]

    r = cfg["risk"]
    hurdle = config.min_ev_on_stake_pct(cfg)
    cap_abs = r["absolute_max_position_usd"]
    max_concurrent = r["max_concurrent_positions"]

    # Concurrency counts DISTINCT MARKETS, not fills. A repeat order on a market
    # already held merges into that position (ledger.open_position), so it takes
    # no extra slot — but the TOTAL exposure to that market is capped, which is
    # what the per-position limit should have meant all along. Polymarket has one
    # net balance per outcome token; there is no second ticket to block.
    open_by_market = {str(p.get("market_id")): p for p in ledger.open_positions()}

    booked = []
    for row in approved:
        mid = str(row["market_id"])
        held = open_by_market.get(mid)
        if held is None and len(open_by_market) >= max_concurrent:
            break

        stake = size(row, ledger.totals(), cfg)
        rate = _rate(row)
        if held is not None:
            # Top up only within the absolute cap — and reserve room for the
            # fee, because `stake` is a GROSS target while the cap applies to
            # ALL-IN cost. Comparing gross room against an all-in total let a
            # top-up overshoot the cap by exactly its own fee, which is how a
            # book ends up with a $21.01 position against a $20 limit.
            room = cap_abs - float(held.get("all_in_usd") or 0.0)
            fee_frac = rate * (1.0 - float(row.get("best_ask") or 0.5))
            stake = min(stake, max(0.0, room / (1.0 + fee_frac)))
        if stake < r["min_position_usd"]:
            continue

        fill = costs.simulate_buy(row["book_asks"], stake, rate=rate)
        if not fill:
            continue
        if held is not None and (float(held.get("all_in_usd") or 0.0) + fill.all_in_usd
                                 > cap_abs + 1e-9):
            continue      # hard guard: never breach the cap on the final numbers
        p = trade_prob(row["forecast"])
        edge = costs.edge_after_costs(p, fill, rate)
        if edge["ev_on_stake_pct"] < hurdle:
            continue          # re-check at FINAL size: slippage may have killed it

        pos_id = f"pos-{row['market_id']}-{int(time.time()*1000)}"
        snap = snapshot_path(pos_id)
        with open(snap, "w") as fh:
            json.dump({
                "captured": ledger.now_iso(),
                "market": {k: row[k] for k in ("market_id", "slug", "question",
                                               "end_date", "fee_rate", "fee_type",
                                               "fees_enabled", "tick")},
                "book_asks": row["book_asks"],
                "book_bids": row["book_bids"],
                "target_stake_usd": stake,
                "fill_as_booked": {
                    "shares": fill.shares, "vwap": fill.vwap,
                    "fee_usd": fill.fee_usd, "all_in_usd": fill.all_in_usd,
                    "levels_used": fill.levels_used,
                },
                "forecast": row["forecast"],
                "edge": edge,
            }, fh, indent=2)

        pos = ledger.open_position(
            market={"id": row["market_id"], "slug": row["slug"],
                    "question": row["question"], "endDate": row["end_date"]},
            token_id=row["token_id_yes"],
            outcome="Yes",
            fill=fill,
            model_prob=p,
            forecast=row["forecast"],
            snapshot_path=snap,
            extra={"target_stake_usd": stake, "edge": edge},
        )
        open_by_market[mid] = pos
        booked.append({"position": pos, "edge": edge})
    return booked


def survival_mode(equity: float, cfg: dict) -> str:
    """Policy ladder. Below the defensive line we stop risking new capital."""
    r = cfg["risk"]
    if equity < r["dead_equity_usd"]:
        return "dead"
    if equity < r["defensive_equity_usd"]:
        return "defensive"
    return "normal"
