"""Journal-versioned, paper-only cancellation when either hedge venue closes."""
import json
import math

from .models import GridError, dec


PAIR_PAUSE_POLICY = "market_closure_cancel_v1"
PRE_CLOSE_SECONDS = 300
VENUE_STATES = {"open", "closed", "close_only", "unknown"}


def fresh(value, now, maximum):
    return type(value) in (int, float) and math.isfinite(value) and -2 <= now - value <= maximum


def venue_states(market, now):
    q, v = market["lighter"], market["var"]
    q_known = "metadata_ts" not in q or fresh(q["metadata_ts"], now, 30)
    lighter = "unknown"
    if q.get("market_open") is False:
        lighter = "closed"
    elif q_known and q.get("market_open", q["ready"]):
        lighter = "close_only" if q.get("close_only", False) else "open"
    cache = market.get("quote_cache", {})
    var = market.get("var_market_state", cache.get("market_state", "unknown"))
    if v:
        if not v.get("market_open", True) or v.get("closes_at") is not None and now >= v["closes_at"]:
            var = "closed"
        elif var == "unknown" and "quote_cache" not in market and "var_market_state" not in market:
            var = "close_only" if v.get("close_only", False) else "open"
    if lighter not in VENUE_STATES or var not in VENUE_STATES:
        raise GridError("Invalid QQQ pair venue state")
    return lighter, var


def close_times(market):
    cache, var = market.get("quote_cache", {}), market["var"] or {}
    result = {"lighter": market["lighter"].get("closes_at"),
              "var": market.get("var_market_closes_at", cache.get("market_closes_at", var.get("closes_at")))}
    for value in result.values():
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise GridError("Invalid QQQ pair closing time")
    return result


def closing_venues(market, now):
    return {venue: at for venue, at in close_times(market).items() if at is not None and now >= at - PRE_CLOSE_SECONDS}


def pause_reason(closed, closing=False):
    if len(closed) == 2:
        return "Both markets closed; both legs paused"
    if closed == ["lighter"]:
        return "Lighter QQQ market closed; both legs paused"
    if closed == ["var"]:
        return "Variational US100 market closed; both legs paused"
    if closing:
        return "Scheduled market close within 5 minutes; both legs paused"
    return "Waiting for both markets and fresh post-closure quotes"


def can_resume(market, now, config, after):
    q, v = market["lighter"], market["var"]
    if (venue_states(market, now) != ("open", "open") or closing_venues(market, now)
            or not market["allow_entries"] or not v or q["gap"] or not q["ready"]):
        return False
    q_source = q.get("source_ts", q["ts"])
    var_max_age = market.get("pricing_policy", {}).get("max_age_seconds", config.settings.max_quote_age_seconds)
    if not fresh(q_source, now, config.settings.max_quote_age_seconds) or not fresh(v["ts"], now, var_max_age):
        return False
    if q_source <= after or v["ts"] <= after:
        return False
    if "metadata_ts" in v and not fresh(v["metadata_ts"], now, 120):
        return False
    return not q.get("close_only", False) and not v.get("close_only", False)


def pair_step(before, market, now, config):
    """Cancel before any fill consumption; old journal frames keep old semantics."""
    from .qqq_hedge import encoded, maker_step
    policy = market.get("pair_pause_policy")
    if policy is None:
        return maker_step(before, market["lighter"], now, config, market["allow_entries"])
    if policy != PAIR_PAUSE_POLICY:
        raise GridError("Unknown QQQ pair pause policy")
    states = venue_states(market, now)
    closed = [venue for venue, state in zip(("lighter", "var"), states) if state == "closed"]
    closing = closing_venues(market, now)
    closed = [venue for venue in ("lighter", "var") if venue in closed or closing.get(venue, math.inf) <= now]
    previous = before.get("pair_pause", {})
    waiting = previous.get("active", False) and not can_resume(market, now, config, previous["resume_after_ts"])
    if closed or closing or waiting:
        account = json.loads(encoded(before))
        pause = account.setdefault("pair_pause", {})
        if not pause.get("active"):
            pause.update(active=True, since=now, resume_after_ts=now, cancelled_entries=0, cancelled_take_profits=0)
        pause.update(reason=pause_reason(closed, bool(closing)), closed_venues=closed,
                     closing_venues=list(closing), lead_seconds=PRE_CLOSE_SECONDS)
        if closed or closing:
            pause["resume_after_ts"] = max(pause["resume_after_ts"], now, *closing.values())
        if closing:
            pause["scheduled_close_ts"] = min(closing.values())
        pause["cancelled_entries"] += sum(o["side"] == "buy" for o in account["orders"])
        pause["cancelled_take_profits"] += sum(o["side"] == "sell" for o in account["orders"])
        account["orders"] = []
        if config.scalper is not None:
            from .qqq_scalper import _finish_entries, _status
            state = account.setdefault("scalper", {"last_entry_ts": None, "last_close_count": 0, "next_batch": 1})
            _finish_entries(account, now)
            for slot in account["slots"]:
                slot.pop("tp_rejection", None)
            state["status"] = _status(account, market["lighter"], now, config, "pair_paused", None)
            state["status"].update(take_profit_blockers=[], suspended_take_profits=[
                {"slot": s["slot"], "quantity": s["qty"], "price": s["tp_price"]}
                for s in account["slots"] if dec(s["qty"]) > 0])
        return account, []
    if previous.get("active"):
        before = json.loads(encoded(before))
        before["pair_pause"].update(active=False, reason="", closed_venues=[], closing_venues=[], resumed_at=now)
        # Recreate orders at this observation. Never replay the closure interval.
        return maker_step(before, {**market["lighter"], "trades": []}, now, config, market["allow_entries"])
    return maker_step(before, market["lighter"], now, config, market["allow_entries"])
