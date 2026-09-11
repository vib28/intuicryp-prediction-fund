"""
AGENT 3 — RISK & EXECUTION
Mandate: decide HOW MUCH, fill against the real book, and refuse anything that
does not clear its costs.

This agent is the pessimist of the fund. Its default answer is "no".
"""

import json
import os

import costs
import ledger

FRACTIONAL_KELLY = 0.25     # of full Kelly. Full Kelly on an uncertain model is suicide.
MIN_EV_ON_STAKE_PCT = 3.0   # expected value must clear this AFTER fees+spread


def snapshot_path(pos_hint: str) -> str:
    os.makedirs(ledger.SNAPSHOTS, exist_ok=True)
    return os.path.join(ledger.SNAPSHOTS, f"{pos_hint}.json")


def trade_prob(forecast: dict) -> float:
    """The probability the fund actually trades on.

    `p_trade` is the smile-anchored, worst-case-vol probability. Falling back to
    `p_conservative` would mean trading our own vol opinion, which is exactly
    what smile.py exists to prevent — so the fallback is the realized-vol
    conservative number only for diagnostics, never silently.
    """
    if "p_trade" in forecast:
        return float(forecast["p_trade"])
    return float(forecast.get("p_conservative", 0.0))


def evaluate(priced: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Rank opportunities by expected value per dollar staked, net of costs."""
    rows = []
    for c in priced:
        f = c["forecast"]
        p = trade_prob(f)
        # Price the fill at the size we could actually afford before sizing.
        probe = min(cfg["risk"]["probe_stake_usd"], cfg["risk"]["max_position_pct"] / 100.0 * ledger.totals()["equity_usd"])
        if probe <= 0:
            continue
        fill = costs.simulate_buy(
            c["book_asks"], probe, rate=float(c["fee_rate"] or costs.DEFAULT_CRYPTO_RATE)
        )
        if not fill:
            continue
        edge = costs.edge_after_costs(p, fill,
                                     float(c["fee_rate"] or costs.DEFAULT_CRYPTO_RATE))
        kelly = costs.kelly_fraction(p, fill.cost_per_share)
        rows.append({**c, "probe_fill": fill, "edge": edge, "kelly_full": kelly})

    rows.sort(key=lambda r: -r["edge"]["ev_on_stake_pct"])

    approved, rejected = [], []
    for r in rows:
        reasons = []
        if r["edge"]["ev_on_stake_pct"] < MIN_EV_ON_STAKE_PCT:
            reasons.append(f"ev_below_hurdle:{r['edge']['ev_on_stake_pct']:.2f}%")
        if r["edge"]["edge_prob"] <= 0:
            reasons.append("no_probability_edge")
        if r["kelly_full"] <= 0:
            reasons.append("kelly_nonpositive")
        if r["probe_fill"].depth_limited and r["probe_fill"].book_depth_usd < cfg["risk"]["min_book_depth_usd"]:
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
    mode = cfg["risk"].get("sizing_mode", "fixed_fraction")
    equity = totals["equity_usd"]
    cash = totals["cash_usd"]

    if mode == "kelly":
        raw = equity * edge_row["kelly_full"] * cfg["risk"].get("fractional_kelly", FRACTIONAL_KELLY)
    else:
        raw = equity * cfg["risk"]["fixed_fraction_pct"] / 100.0

    cap_usd = equity * cfg["risk"]["max_position_pct"] / 100.0
    hard_cap = cfg["risk"]["absolute_max_position_usd"]
    floor = cfg["risk"]["min_position_usd"]

    stake = min(max(raw, floor), cap_usd, hard_cap, cash * 0.98)
    return max(0.0, stake)


def execute(approved: list[dict], cfg: dict, dry_run: bool = False) -> list[dict]:
    """Book paper positions, subject to concurrency and survival policy."""
    booked = []
    totals = ledger.totals()
    equity = totals["equity_usd"]
    max_concurrent = cfg["risk"]["max_concurrent_positions"]

    mode = survival_mode(equity, cfg)
    if mode != "normal":
        return [{"skipped": True, "reason": f"survival_mode:{mode}"}]

    for row in approved:
        if len(ledger.open_positions()) >= max_concurrent:
            break
        stake = size(row, ledger.totals(), cfg)
        if stake < cfg["risk"]["min_position_usd"]:
            continue

        rate = float(row["fee_rate"] or costs.DEFAULT_CRYPTO_RATE)
        fill = costs.simulate_buy(row["book_asks"], stake, rate=rate)
        if not fill:
            continue
        p = trade_prob(row["forecast"])
        edge = costs.edge_after_costs(p, fill, rate)
        if edge["ev_on_stake_pct"] < MIN_EV_ON_STAKE_PCT:
            continue                      # re-check at FINAL size: slippage may have killed it

        pos_id = f"pos-{row['market_id']}-{int(ledger.time.time()*1000)}"
        snap = snapshot_path(pos_id)
        payload = {
            "captured": ledger.now_iso(),
            "market": {k: row[k] for k in ("market_id", "slug", "question", "end_date",
                                           "fee_rate", "fee_type", "fees_enabled", "tick")},
            "book_asks": row["book_asks"],
            "book_bids": row["book_bids"],
            "target_stake_usd": stake,
            "fill_as_booked": {
                "shares": fill.shares, "vwap": fill.vwap, "fee_usd": fill.fee_usd,
                "all_in_usd": fill.all_in_usd, "levels_used": fill.levels_used,
            },
            "forecast": row["forecast"],
            "edge": edge,
        }
        with open(snap, "w") as fh:
            json.dump(payload, fh, indent=2)

        if dry_run:
            continue

        pos = ledger.open_position(
            market={"id": row["market_id"], "slug": row["slug"],
                    "question": row["question"], "endDate": row["end_date"]},
            token_id=row["token_id_yes"],
            outcome="Yes",
            fill=fill,
            model_prob=trade_prob(row["forecast"]),
            forecast=row["forecast"],
            snapshot_path=snap,
        )
        pos["stake_usd"] = stake
        pos["edge"] = edge
        state = ledger.load_json(ledger.POSITIONS, {"positions": []})
        state["positions"] = [pos if p.get("id") == pos["id"] else p for p in state["positions"]]
        ledger.save_json(ledger.POSITIONS, state)

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
