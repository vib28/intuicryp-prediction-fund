"""
Table — the full trade table, with costs and burn, in one view.

Answers "table" with every trade the fund has taken (open and settled),
what KIND of prediction each was, what it cost in fees, and what it cost to
run — because a P&L that ignores the burn is fiction.

Renders:
  1. capital / burn / target header
  2. every trade: prediction type, strike, entry -> exit-or-mark, stake, fee, PnL
  3. totals: realized, unrealized, net, fees paid, burn charged (split tokens/VPS)
  4. burn breakdown and runway

Open positions are marked to the live book midpoint when it can be fetched;
if not, they fall back to cost basis (no paper profit is claimed on a guess).
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ledger      # noqa: E402
import venue       # noqa: E402

BURN_WEEKLY = 15.0
BURN_TOKENS_WEEKLY = 10.0
BURN_VPS_WEEKLY = 5.0


def horizon_bucket(days: float | None) -> str:
    """Label the market by how long it lives — the thing the user trades.

    Buckets on MINUTES, not days: a 15-minute market is 0.0104 days, which sits
    exactly on a day-based boundary and mislabels as '1h'.
    """
    if days is None:
        return "?"
    mins = days * 1440.0
    if mins <= 20:
        return "15min"
    if mins <= 75:
        return "1h"
    if mins <= 480:
        return "6h"
    if mins <= 2160:
        return "daily"
    if mins <= 11520:
        return "weekly"
    return "monthly"


_KIND = {"digital_above": "above", "touch_up": "reach", "touch_down": "dip"}


def market_type(pos: dict) -> str:
    """e.g. 'BTC daily above', 'ETH weekly dip', 'BTC 15min updown'."""
    f = pos.get("forecast") or {}
    sym = (f.get("symbol") or pos.get("symbol") or "?").replace("USDT", "")
    family = f.get("family") or pos.get("family") or "?"
    days = f.get("days")
    if days is None and pos.get("end_date") and pos.get("opened"):
        from datetime import datetime
        try:
            end = datetime.fromisoformat(pos["end_date"].replace("Z", "+00:00"))
            op = datetime.fromisoformat(pos["opened"])
            days = (end - op).total_seconds() / 86400.0
        except ValueError:
            days = None
    return f"{sym} {horizon_bucket(days)} {_KIND.get(family, family)}"


def mark(pos: dict) -> float | None:
    """Live midpoint for an open position, or None if unavailable."""
    tok = pos.get("token_id")
    if not tok:
        return None
    try:
        book = venue.order_book(tok)
        bid, ask = venue.best_bid_ask(book)
        if bid is None or ask is None:
            return None
        return 0.5 * (bid + ask)
    except Exception:                              # noqa: BLE001
        return None


def build() -> dict:
    acct = ledger.load_json(ledger.ACCOUNT, {})
    positions = sorted(ledger.all_positions(), key=lambda p: p.get("opened") or "")
    rows = []
    real = unreal = fees = 0.0

    for i, p in enumerate(positions, 1):
        stake = float(p.get("all_in_usd") or 0)
        fee = float(p.get("fee_usd") or 0)
        fees += fee
        settled = p.get("status") == "settled"
        if settled:
            pnl = float(p.get("pnl_usd") or 0)
            real += pnl
            exit_disp = f"{p.get('payout_usd', 0) / float(p['shares']):.4f}" if p.get("shares") else "-"
            exit_disp = "1.0000" if p.get("won") else "0.0000"
            status = "WIN" if p.get("won") else "LOSS"
        else:
            m = mark(p)
            entry = float(p.get("cost_per_share") or 0)
            shares = float(p.get("shares") or 0)
            pnl = (m - entry) * shares if m is not None else 0.0
            unreal += pnl
            exit_disp = f"{m:.4f}" if m is not None else "n/a"
            status = "open"
        rows.append({
            "n": i,
            "type": market_type(p),
            "slug": p.get("slug") or "",
            "strike": (p.get("forecast") or {}).get("strike"),
            "entry": float(p.get("cost_per_share") or 0),
            "exit": exit_disp,
            "stake": stake,
            "fee": fee,
            "pnl": pnl,
            "pnl_pct": 100.0 * pnl / stake if stake else 0.0,
            "status": status,
            "opened": (p.get("opened") or "")[:16].replace("T", " "),
        })

    burn = float(acct.get("burn_accrued_usd", 0.0))
    equity = ledger.totals()["equity_usd"]
    return {
        "rows": rows,
        "realized": real,
        "unrealized": unreal,
        "fees": fees,
        "burn": burn,
        "burn_tokens": burn * (BURN_TOKENS_WEEKLY / BURN_WEEKLY),
        "burn_vps": burn * (BURN_VPS_WEEKLY / BURN_WEEKLY),
        "equity": equity,
        "start": float(acct.get("starting_equity_usd", ledger.STARTING_EQ)),
        "trades": len(positions),
        "wins": sum(1 for p in positions if p.get("status") == "settled" and p.get("won")),
        "losses": sum(1 for p in positions if p.get("status") == "settled" and not p.get("won")),
        "open_count": len(ledger.open_positions()),
    }


def render(d: dict) -> str:
    out = []
    net = d["realized"] + d["unrealized"]
    out.append("FUND TABLE — IntuiCryp Prediction Fund")
    out.append("=" * 100)
    out.append(f"CAPITAL   start ${d['start']:.2f}   equity ${d['equity']:.4f}   "
               f"net {d['equity'] - d['start']:+.4f} ({100*(d['equity']-d['start'])/d['start']:+.2f}%)")
    out.append(f"BURN      ${BURN_WEEKLY:.2f}/wk = ${BURN_TOKENS_WEEKLY:.2f} tokens + ${BURN_VPS_WEEKLY:.2f} VPS"
               f"   charged so far ${d['burn']:.4f} "
               f"(${d['burn_tokens']:.4f} tokens + ${d['burn_vps']:.4f} VPS)")
    runway = (d["equity"] / BURN_WEEKLY) if BURN_WEEKLY else float("inf")
    out.append(f"TARGET    $1,000 (10x)   runway at zero edge: {runway:.1f} weeks")
    out.append("")

    if not d["rows"]:
        out.append("TRADES    none yet — no dislocation has cleared the cost hurdle.")
    else:
        out.append(f"TRADES ({d['trades']}: {d['open_count']} open, {d['wins']}W/{d['losses']}L settled)")
        out.append(f"{'#':>2} {'opened':16} {'prediction type':22} {'market':34} "
                   f"{'strike':>9} {'entry':>6} {'exit':>7} {'stake':>7} {'fee':>7} "
                   f"{'pnl $':>9} {'pnl %':>8} {'status':>6}")
        out.append("-" * 150)
        for r in d["rows"]:
            strike = f"{r['strike']:,.0f}" if r["strike"] else "-"
            out.append(f"{r['n']:>2} {r['opened']:16} {r['type']:22} {r['slug'][:34]:34} "
                       f"{strike:>9} {r['entry']:>6.4f} {str(r['exit']):>7} {r['stake']:>7.2f} "
                       f"{r['fee']:>7.4f} {r['pnl']:>+9.4f} {r['pnl_pct']:>+7.2f}% {r['status']:>6}")
    out.append("")
    out.append("TOTALS")
    out.append(f"  realized PnL        ${d['realized']:+.4f}")
    out.append(f"  unrealized PnL      ${d['unrealized']:+.4f}")
    out.append(f"  NET P&L             ${net:+.4f}")
    out.append(f"  fees paid (venue)   ${d['fees']:.4f}  ({100*d['fees']/d['equity']:.3f}% of equity)")
    out.append(f"  burn charged        ${d['burn']:.4f}  (${d['burn_tokens']:.4f} tokens + ${d['burn_vps']:.4f} VPS)")
    out.append(f"  net of everything   ${d['equity'] - d['start']:+.4f}")
    out.append("=" * 100)
    return "\n".join(out)


if __name__ == "__main__":
    print(render(build()))
