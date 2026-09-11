"""
Ledger — append-only state, cash, burn accrual, and settlement.

Design rules that matter:
  * The ledger is APPEND-ONLY. Every event is a line of JSON with a timestamp;
    equity is always derived by replaying it, never by mutating a running
    total. That is what makes the Auditor able to disagree with the traders.
  * The $15/week burn is a real cash outflow accruing per second, not a
    weekly afterthought. Burn that is not modelled cannot be survived.
  * Positions are held to resolution. There is no exit path in this module —
    no close_position(), deliberately. costs.py explains why: exiting early
    costs 4-42% of the stake.
"""

import json
import os
import time
from datetime import datetime, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
LEDGER = os.path.join(STATE_DIR, "ledger.jsonl")
ACCOUNT = os.path.join(STATE_DIR, "account.json")
POSITIONS = os.path.join(STATE_DIR, "positions.json")
SNAPSHOTS = os.path.join(STATE_DIR, "snapshots")

STARTING_EQ = 100.0
BURN_WEEKLY = 15.0
SECONDS_PER_WEEK = 7 * 24 * 3600
BURN_PER_SECOND = BURN_WEEKLY / SECONDS_PER_WEEK


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_state() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS, exist_ok=True)
    if not os.path.exists(ACCOUNT):
        save_json(ACCOUNT, {
            "created": now_iso(),
            "starting_equity_usd": STARTING_EQ,
            "cash_usd": STARTING_EQ,
            "burn_accrued_usd": 0.0,
            "last_burn_ts": time.time(),
        })
    if not os.path.exists(POSITIONS):
        save_json(POSITIONS, {"positions": []})
    if not os.path.exists(LEDGER):
        open(LEDGER, "a").close()


def save_json(path: str, obj) -> None:
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


def accrue_burn(apply: bool = True) -> float:
    """Book the burn that has elapsed since the last accrual.

    Returns the amount accrued this call. Applied to cash, because a burn
    that never leaves the account is a fantasy.
    """
    acct = load_json(ACCOUNT, {})
    last = float(acct.get("last_burn_ts") or time.time())
    elapsed = max(0.0, time.time() - last)
    amount = elapsed * BURN_PER_SECOND
    if apply and amount > 0:
        acct["cash_usd"] = float(acct.get("cash_usd", STARTING_EQ)) - amount
        acct["burn_accrued_usd"] = float(acct.get("burn_accrued_usd", 0.0)) + amount
        acct["last_burn_ts"] = time.time()
        save_json(ACCOUNT, acct)
        append_event("burn", amount_usd=round(amount, 6), elapsed_s=round(elapsed, 1))
    return amount


def open_position(*, market: dict, token_id: str, outcome: str, fill,
                  model_prob: float, forecast: dict, snapshot_path: str) -> dict:
    """Book a paper fill. Cash leaves the account at the ALL-IN cost."""
    acct = load_json(ACCOUNT, {})
    acct["cash_usd"] = float(acct.get("cash_usd", STARTING_EQ)) - fill.all_in_usd
    save_json(ACCOUNT, acct)

    pos = {
        "id": f"pos-{int(time.time()*1000)}",
        "opened": now_iso(),
        "market_id": str(market.get("id")),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "token_id": token_id,
        "outcome": outcome,
        "end_date": market.get("endDate"),
        "shares": fill.shares,
        "vwap": fill.vwap,
        "cost_per_share": fill.cost_per_share,
        "all_in_usd": fill.all_in_usd,
        "fee_usd": fill.fee_usd,
        "levels_used": fill.levels_used,
        "book_depth_usd": fill.book_depth_usd,
        "model_prob": model_prob,
        "forecast": forecast,
        "snapshot": snapshot_path,
        "status": "open",
    }
    state = load_json(POSITIONS, {"positions": []})
    state["positions"].append(pos)
    save_json(POSITIONS, state)
    append_event("open", **{k: pos[k] for k in
                            ("id", "market_id", "slug", "outcome", "shares",
                             "cost_per_share", "all_in_usd", "fee_usd", "model_prob")})
    return pos


def settle_position(pos: dict, won: bool, evidence: dict) -> dict:
    """Settle against the real oracle outcome. Win pays $1/share, free."""
    payout = float(pos["shares"]) if won else 0.0
    pnl = payout - float(pos["all_in_usd"])
    acct = load_json(ACCOUNT, {})
    acct["cash_usd"] = float(acct.get("cash_usd", STARTING_EQ)) + payout
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
    state = load_json(POSITIONS, {"positions": []})
    return [p for p in state["positions"] if p.get("status") == "open"]


def all_positions() -> list[dict]:
    state = load_json(POSITIONS, {"positions": []})
    return state["positions"]


def totals(marks: dict | None = None) -> dict:
    """Equity by replay. `marks` maps position id -> current mid price.

    Unmarked positions fall back to their cost basis (conservative: no paper
    profit is claimed before the oracle has spoken).
    """
    acct = load_json(ACCOUNT, {})
    cash = float(acct.get("cash_usd", STARTING_EQ))
    pos_value = 0.0
    for p in open_positions():
        mark = (marks or {}).get(p["id"], p["cost_per_share"])
        pos_value += float(p["shares"]) * float(mark)
    equity = cash + pos_value
    realized = sum(float(p.get("pnl_usd") or 0.0)
                   for p in all_positions() if p.get("status") == "settled")
    return {
        "cash_usd": cash,
        "positions_value_usd": pos_value,
        "equity_usd": equity,
        "realized_pnl_usd": realized,
        "burn_accrued_usd": float(acct.get("burn_accrued_usd", 0.0)),
        "starting_equity_usd": float(acct.get("starting_equity_usd", STARTING_EQ)),
        "open_count": len(open_positions()),
    }
