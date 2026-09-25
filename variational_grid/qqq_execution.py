"""Paper GTT take-profit submission and one-time arrival against visible depth."""
import math

from .models import D, dec


def submit_take_profit(account, slot, quantity, market, now, settings):
    """Accept an exit intent even when its limit already crosses the bid."""
    price = dec(slot["tp_price"])
    if quantity < dec(market["min_qty"]):
        return "below_min_quantity"
    if quantity * price < dec(market.get("min_notional", "0")):
        return "below_min_notional"
    if quantity % dec(market["size_step"]):
        return "quantity_off_step"
    account["orders"].append({"id": account["next_order"], "slot": slot["slot"], "side": "sell",
                              "price": str(price), "remaining": str(quantity), "queue": None,
                              "submitted_ts": now, "active_ts": now + settings.maker_latency_ms / 1000,
                              "cancel_ts": None, "time_in_force": "GTT", "activation_pending": True})
    account["next_order"] += 1
    return None


def _book_time(market, now, settings):
    # Live source time is HTTP Date minus cache Age, not the later trades read.
    source = market.get("source_ts", market["ts"] if not market.get("source") else None)
    if (isinstance(source, bool) or not isinstance(source, (int, float)) or not math.isfinite(source)
            or not 0 <= now - source <= settings.max_quote_age_seconds):
        return None
    return source


def activate_take_profits(account, market, now, config):
    """Consume one shared book at arrival; any unfilled remainder then rests."""
    from .qqq_hedge import book_fill, floor
    pending = [o for o in account["orders"] if o.get("activation_pending")]
    if not pending:
        return []
    source = _book_time(market, now, config.settings)
    state = account["scalper"]
    if source is None or source <= state.get("last_tp_book_ts", -1):
        return []
    eligible = [o for o in pending if source >= o["active_ts"] and source > o["submitted_ts"]]
    if not eligible:
        return []
    # The market adapter aggregates levels. Recheck coherence before treating
    # any visible depth as executable; historical/synthetic frames may differ.
    bids = sorted(([dec(p), dec(q)] for p, q in market["bids"] if dec(q) > 0), reverse=True)
    asks = sorted(([dec(p), dec(q)] for p, q in market["asks"] if dec(q) > 0))
    if not bids or not asks or bids[0][0] != dec(market["bid"]) or asks[0][0] != dec(market["ask"]) or bids[0][0] >= asks[0][0]:
        return []
    slots = {s["slot"]: s for s in account["slots"]}
    fills = []
    for order in sorted(eligible, key=lambda o: (dec(o["price"]), o["id"])):
        slot, limit = slots[order["slot"]], dec(order["price"])
        for level in bids:
            price, available = level
            if price < limit or not dec(order["remaining"]):
                break
            quantity = floor(min(available, dec(order["remaining"]), dec(slot["qty"])), market["size_step"])
            if not quantity:
                continue
            fill = book_fill(account, "qqq", -quantity, price, config.settings.lighter_fee_bps,
                             now, "taker_take_profit", slot["slot"])
            fill.update(maker_model=config.scalper.model, liquidity="taker",
                        quote_source="lighter_public_book", quote_source_ts=source)
            fills.append(fill)
            level[1] -= quantity
            order["remaining"] = str(dec(order["remaining"]) - quantity)
            slot["qty"] = str(dec(slot["qty"]) - quantity)
        order["activation_pending"] = False
        # No history from this observation can execute newly resting quantity.
        order["active_ts"] = now
        order["rested_ts"] = now
        order["queue"] = (str(sum((qty for price, qty in asks if price == limit), D(0))
                              * dec(config.settings.queue_multiplier)) if limit <= asks[-1][0] else None)
    state["last_tp_book_ts"] = source
    account["orders"] = [o for o in account["orders"] if dec(o["remaining"]) > 0]
    return fills


def take_profit_coverage(account):
    """Report exact uncovered inventory, including exchange minimum failures."""
    uncovered = []
    for slot in account["slots"]:
        if slot["entry_pending"] or not dec(slot["qty"]):
            continue
        covered = sum((dec(o["remaining"]) for o in account["orders"]
                       if o["slot"] == slot["slot"] and o["side"] == "sell"), D(0))
        if dec(slot["qty"]) > covered:
            uncovered.append({"slot": slot["slot"], "quantity": str(dec(slot["qty"]) - covered),
                              "reason": slot.get("tp_rejection", "awaiting_submission")})
    return uncovered
