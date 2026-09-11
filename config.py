"""
Config — the single source of truth for the fund's policy.

Every module reads policy from here. Two failures this prevents, both of which
had already happened in this codebase:

  * CONSTANTS DUPLICATED IN THREE PLACES. The weekly burn lived in `config.json`,
    again in `ledger.py` (BURN_WEEKLY) and a third time in `table.py`
    (BURN_WEEKLY / BURN_TOKENS_WEEKLY / BURN_VPS_WEEKLY). Editing one had no
    effect on the others, so a burn change would have been silently half-applied.
  * A CONFIG KEY NOTHING READ. `risk.min_ev_on_stake_pct` was shadowed by a
    module constant in `risk.py`, so editing the config changed nothing at all —
    the most dangerous kind of config bug, because it looks like it worked.

Policy numbers belong in one place, and code that uses a policy number must read
it from that place. `validate()` fails loudly at load time if a required key is
missing, so a bad edit surfaces as an error rather than as a quietly different
strategy.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "config.json")

# Only the keys the code actually REQUIRES. Documentation-only keys (such as
# `costs.formula`, `risk.sizing_note`) are deliberately not listed — they are
# commentary for the reader, not policy the code consumes.
REQUIRED = (
    "fund.name", "fund.venue", "fund.mode", "fund.capital_usd", "fund.target_usd",
    "fund.exit_policy",
    "burn.vps_weekly_usd", "burn.tokens_weekly_usd", "burn.total_weekly_usd",
    "scan.pages", "scan.interval_minutes", "scan.horizon_cadence_multiple",
    "scan.daily_assets", "scan.daily_days_ahead", "scan.search_queries",
    "universe.min_volume_24h_usd", "universe.min_liquidity_usd",
    "universe.max_spread", "universe.min_days_to_resolution",
    "universe.max_days_to_resolution", "universe.min_ask", "universe.max_ask",
    "risk.sizing_mode", "risk.fixed_fraction_pct", "risk.max_position_pct",
    "risk.absolute_max_position_usd", "risk.min_position_usd",
    "risk.probe_stake_usd", "risk.min_book_depth_usd",
    "risk.max_concurrent_positions", "risk.defensive_equity_usd",
    "risk.dead_equity_usd", "risk.fractional_kelly", "risk.min_ev_on_stake_pct",
)

_cache = {"stamp": None, "cfg": None}


def _dig(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def validate(cfg: dict) -> dict:
    missing = [k for k in REQUIRED if _dig(cfg, k) is None]
    if missing:
        raise ValueError("config.json is missing required key(s): " + ", ".join(missing))
    return cfg


def load(path: str | None = None) -> dict:
    """Parsed config. Re-read from disk only when the file has changed."""
    p = path or PATH
    shared = os.path.abspath(p) == PATH
    try:
        stamp = os.path.getmtime(p)
    except OSError:
        stamp = None
    if shared and _cache["cfg"] is not None and _cache["stamp"] == stamp:
        return _cache["cfg"]
    with open(p) as fh:
        cfg = validate(json.load(fh))
    if shared:
        _cache.update(stamp=stamp, cfg=cfg)
    return cfg


# ---- derived policy values -------------------------------------------------
# Callers ask for the VALUE rather than re-deriving it from a raw dict, so there
# is exactly one place where each policy number is interpreted.

def capital(cfg: dict | None = None) -> float:
    return float((cfg or load())["fund"]["capital_usd"])


def target(cfg: dict | None = None) -> float:
    return float((cfg or load())["fund"]["target_usd"])


def burn_total_weekly(cfg: dict | None = None) -> float:
    return float((cfg or load())["burn"]["total_weekly_usd"])


def burn_tokens_weekly(cfg: dict | None = None) -> float:
    return float((cfg or load())["burn"]["tokens_weekly_usd"])


def burn_vps_weekly(cfg: dict | None = None) -> float:
    return float((cfg or load())["burn"]["vps_weekly_usd"])


def burn_split(cfg: dict | None = None) -> tuple[float, float]:
    """(tokens, vps) as fractions of the weekly burn — for attributing burn."""
    cfg = cfg or load()
    tot = burn_total_weekly(cfg) or 1.0
    return burn_tokens_weekly(cfg) / tot, burn_vps_weekly(cfg) / tot


def min_ev_on_stake_pct(cfg: dict | None = None) -> float:
    return float((cfg or load())["risk"]["min_ev_on_stake_pct"])
