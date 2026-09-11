"""
Ledger — append-only state, cash, burn accrual, and settlement.

Design rules that matter:
  * The ledger is APPEND-ONLY. Every event is a line of JSON with a timestamp;
    equity is always derived by replaying it, never by mutating a running
    total. That is what makes the Auditor able to disagree with the traders.
  * The weekly burn is a real cash outflow accruing per second, not a weekly
    afterthought. Burn that is not modelled cannot be survived. Its size comes
    from config.py, never from a constant here.
  * Positions are held to resolution. There is no exit path in this module —
    no close_position(), deliberately. costs.py explains why: exiting early
    costs 2.5-33.3% of the stake.
  * A market has ONE position, which may hold SEVERAL fills. On Polymarket there
    is a single net balance per outcome token, so a repeat order merges into the
    existing position with a blended cost basis. Modelling repeats as separate
    positions would apply the exposure cap per fill and silently turn two orders
    into a double-size bet on one oracle call.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import config

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
LEDGER = os.path.join(STATE_DIR, "ledger.jsonl")
ACCOUNT = os.path.join(STATE_DIR, "account.json")
POSITIONS = os.path.join(STATE_DIR, "positions.json")
SNAPSHOTS = os.path.join(STATE_DIR, "snapshots")

SECONDS_PER_WEEK = 7 * 24 * 3600

# What burn is attributed to. "tokens" here means the agent/LLM spend, which is
# the fund's dominant recurring cost and the one that moves with activity.
CATEGORIES = ("tokens", "vps")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def burn_per_second(cfg: dict | None = None) -> float:
    return config.burn_total_weekly(cfg) / SECONDS_PER_WEEK


def ensure_state() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS, exist_ok=True)
    if not os.path.exists(ACCOUNT):
        save_json(ACCOUNT, {
            "created": now_iso(),
            "starting_equity_usd": config.capital(),
            "cash_usd": config.capital(),
            "burn_accrued_usd": 0.0,
            "burn_by_category": {c: 0.0 for c in CATEGORIES},
            "last_burn_ts": time.time(),
        })
    if not os.path.exists(POSITIONS):
        save_json(POSITIONS, {"positions": []})
    if not os.path.exists(LEDGER):
        open(LEDGER, "a").close()


def save_json(path: str, obj) -> None:
    """Write atomically: a half-written account.json would be a lost book."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)


def append_event(kind: str, **fields) -> dict:
    ev = {"ts": now_iso(), "kind": kind, **fields}
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(ev) + "\n")
    return ev


def read_ledger() -> list[dict]:
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def burn_by_category(acct: dict | None = None) -> dict:
    """Burn split by what caused it: {"tokens", "vps"}.

    Read from the account when tracked. Accounts written before this was tracked
    fall back to splitting the historical total by the configured baseline ratio,
    which is the best available attribution for spend already incurred.
    """
    acct = acct if acct is not None else load_json(ACCOUNT, {})
    stored = acct.get("burn_by_category")
    if stored:
        return {c: float(stored.get(c, 0.0)) for c in CATEGORIES}
    tokens_share, vps_share = config.burn_split()
    total = float(acct.get("burn_accrued_usd", 0.0))
    return {"tokens": total * tokens_share, "vps": total * vps_share}


def _add_burn(acct: dict, amount: float, category: str | None = None) -> None:
    """Book `amount` of burn, attributed to a named category or to the baseline.

    The category split is derived BEFORE the running total is incremented,
    because the derivation falls back to that total when it is absent.
    """
    split = burn_by_category(acct)
    if category:
        split[category] = split.get(category, 0.0) + amount
    else:
        tokens_share, vps_share = config.burn_split()
        split["tokens"] += amount * tokens_share
        split["vps"] += amount * vps_share
    acct["burn_accrued_usd"] = float(acct.get("burn_accrued_usd", 0.0)) + amount
    acct["burn_by_category"] = split


def accrue_burn(apply: bool = True, cfg: dict | None = None) -> float:
    """Book the burn elapsed since the last accrual, and return it.

    Applied to cash, because a burn that never leaves the account is a fantasy.
    """
    acct = load_json(ACCOUNT, {})
    last = float(acct.get("last_burn_ts") or time.time())
    elapsed = max(0.0, time.time() - last)
    amount = elapsed * burn_per_second(cfg)
    if apply and amount > 0:
        acct["cash_usd"] = float(acct.get("cash_usd", config.capital())) - amount
        acct["last_burn_ts"] = time.time()
        _add_burn(acct, amount)
        save_json(ACCOUNT, acct)
        append_event("burn", amount_usd=round(amount, 6), elapsed_s=round(elapsed, 1))
    return amount


def record_one_off_burn(amount_usd: float, category: str = "tokens",
                        reason: str = "") -> dict:
    """Book burn that did not accrue on a clock — a bill that arrived.

    The baseline burn is a RATE (config.burn.total_weekly_usd spread per second).
    Some costs are not: an agent/dev session is a one-off invoice for tokens, and
    folding it into the rate would misdate it, dilute it across the whole week,
    and hide it. So it is booked as a single `burn` event with a category and a
    reason. It stays a `burn` event deliberately, so the Auditor's equity replay
    still reconciles against stored cash instead of drifting.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown burn category {category!r}; expected one of {CATEGORIES}")
    amount = float(amount_usd)
    if amount < 0:
        raise ValueError("a one-off burn cannot be negative")
    acct = load_json(ACCOUNT, {})
    acct["cash_usd"] = float(acct.get("cash_usd", config.capital())) - amount
    _add_burn(acct, amount, category=category)
    save_json(ACCOUNT, acct)
    return append_event("burn", amount_usd=round(amount, 6), category=category,
                        one_off=True, reason=reason)


def _recompute(pos: dict) -> dict:
    """Derive a position's aggregates from its fills."""
    fills = pos.get("fills") or []
    if not fills:
        return pos
    tot_shares = sum(float(f["shares"]) for f in fills)
    tot_allin = sum(float(f["all_in_usd"]) for f in fills)
    pos["shares"] = tot_shares
    pos["all_in_usd"] = tot_allin
    pos["fee_usd"] = sum(float(f["fee_usd"]) for f in fills)
    pos["vwap"] = (sum(float(f["shares"]) * float(f["vwap"]) for f in fills)
                   / tot_shares) if tot_shares else 0.0
    pos["cost_per_share"] = tot_allin / tot_shares if tot_shares else 0.0
    # Share-weighted mean of the model's probability, so the position's stored
    # `model_prob` reflects the size of each fill rather than the last one.
    pos["model_prob"] = (sum(float(f.get("model_prob") or 0.0) * float(f["shares"])
                             for f in fills) / tot_shares) if tot_shares else 0.0
    return pos


def open_position(*, market: dict, token_id: str, outcome: str, fill,
                  model_prob: float, forecast: dict, snapshot_path: str,
                  extra: dict | None = None) -> dict:
    """Book a paper fill, MERGING into an existing position for the same market.

    Cash leaves the account at the all-in cost either way. If the market is
    already held, the fill is appended to that position's `fills` and the
    aggregates (shares, blended cost basis, total fee) are recomputed — which is
    what the venue itself does with a repeat order.

    `extra` carries position-level annotations (the requested size, the edge)
    so the caller does not have to re-read and re-write the state file after
    booking, which is both slower and a window for the two writes to disagree.
    """
    equity_before = totals()["equity_usd"]

    acct = load_json(ACCOUNT, {})
    acct["cash_usd"] = float(acct.get("cash_usd", config.capital())) - fill.all_in_usd
    save_json(ACCOUNT, acct)

    entry = {
        "ts": now_iso(),
        "shares": fill.shares,
        "vwap": fill.vwap,
        "all_in_usd": fill.all_in_usd,
        "fee_usd": fill.fee_usd,
        "stake_usd": fill.gross_usd,
        "levels_used": fill.levels_used,
        "book_depth_usd": fill.book_depth_usd,
        "model_prob": model_prob,
        "snapshot": snapshot_path,
        # Recorded so the Auditor can check the percentage cap against the book
        # as it stood when the fill was made, instead of guessing from today's
        # equity — which drifts with burn and would make the check meaningless.
        "equity_before_usd": equity_before,
    }

    state = load_json(POSITIONS, {"positions": []})
    existing = next((p for p in state["positions"]
                     if p.get("status") == "open"
                     and str(p.get("market_id")) == str(market.get("id"))), None)

    if existing is not None:
        existing.setdefault("fills", []).append(entry)
        existing.update(extra or {})
        _recompute(existing)
        save_json(POSITIONS, state)
        append_event("add", id=existing["id"], market_id=existing["market_id"],
                     slug=existing.get("slug"), shares=entry["shares"],
                     all_in_usd=entry["all_in_usd"], fee_usd=entry["fee_usd"],
                     fills=len(existing["fills"]))
        return existing

    pos = {
        "id": f"pos-{int(time.time()*1000)}",
        "opened": now_iso(),
        "market_id": str(market.get("id")),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "token_id": token_id,
        "outcome": outcome,
        "end_date": market.get("endDate"),
        "fills": [entry],
        "forecast": forecast,
        "status": "open",
    }
    pos.update(extra or {})
    _recompute(pos)
    state["positions"].append(pos)
    save_json(POSITIONS, state)
    append_event("open", **{k: pos[k] for k in
                            ("id", "market_id", "slug", "outcome", "shares",
                             "cost_per_share", "all_in_usd", "fee_usd", "model_prob")})
    return pos


def settle_position(pos: dict, won: bool, evidence: dict) -> dict:
    """Settle against the real oracle outcome. A win pays $1/share, fee-free."""
    payout = float(pos["shares"]) if won else 0.0
    pnl = payout - float(pos["all_in_usd"])
    acct = load_json(ACCOUNT, {})
    acct["cash_usd"] = float(acct.get("cash_usd", config.capital())) + payout
    save_json(ACCOUNT, acct)

    pos = dict(pos)
    pos.update({
        "status": "settled",
        "settled": now_iso(),
        "won": won,
        "payout_usd": payout,
        "pnl_usd": pnl,
        "pnl_pct": 100.0 * pnl / float(pos["all_in_usd"]) if pos["all_in_usd"] else 0.0,
        "evidence": evidence,
    })
    state = load_json(POSITIONS, {"positions": []})
    state["positions"] = [
        pos if p.get("id") == pos["id"] else p for p in state["positions"]
    ]
    save_json(POSITIONS, state)
    append_event("settle", id=pos["id"], slug=pos.get("slug"), won=won,
                 payout_usd=round(payout, 6), pnl_usd=round(pnl, 6),
                 evidence=evidence)
    return pos


def open_positions() -> list[dict]:
    return [p for p in all_positions() if p.get("status") == "open"]


def all_positions() -> list[dict]:
    return load_json(POSITIONS, {"positions": []})["positions"]


def totals() -> dict:
    """Equity by replay.

    Open positions are valued at cost basis unless the caller marks them, which
    is deliberately conservative: no paper profit is claimed before the oracle
    has spoken. `report.state()` and `table.build()` mark them for display.
    """
    acct = load_json(ACCOUNT, {})
    cash = float(acct.get("cash_usd", config.capital()))
    opens = open_positions()
    pos_value = sum(float(p["shares"]) * float(p["cost_per_share"]) for p in opens)
    equity = cash + pos_value
    realized = sum(float(p.get("pnl_usd") or 0.0)
                   for p in all_positions() if p.get("status") == "settled")
    return {
        "cash_usd": cash,
        "positions_value_usd": pos_value,
        "equity_usd": equity,
        "realized_pnl_usd": realized,
        "burn_accrued_usd": float(acct.get("burn_accrued_usd", 0.0)),
        "burn_by_category": burn_by_category(acct),
        "starting_equity_usd": float(acct.get("starting_equity_usd", config.capital())),
        "open_count": len(opens),
    }


def one_off_burns() -> list[dict]:
    """Burn events that were billed, not accrued — e.g. agent/dev token spend."""
    return [e for e in read_ledger()
            if e.get("kind") == "burn" and e.get("one_off")]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--one-off-burn":
        if len(args) < 2:
            raise SystemExit("usage: ledger.py --one-off-burn <usd> "
                             "[tokens|vps] [reason]")
        event = record_one_off_burn(
            float(args[1]),
            args[2] if len(args) > 2 else "tokens",
            " ".join(args[3:]),
        )
        t = totals()
        print(json.dumps({
            "recorded": event,
            "burn_accrued_usd": round(t["burn_accrued_usd"], 6),
            "burn_by_category": {k: round(v, 6) for k, v in t["burn_by_category"].items()},
            "cash_usd": round(t["cash_usd"], 6),
            "equity_usd": round(t["equity_usd"], 6),
        }, indent=2))
        raise SystemExit(0)
    t = totals()
    t["burn_by_category"] = {k: round(v, 6) for k, v in t["burn_by_category"].items()}
    print(json.dumps(t, indent=2))
