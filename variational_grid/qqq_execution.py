"""Paper limit exits: GTT for normal sizes, IOC for otherwise stranded dust."""
import math

from .models import D, dec


TAKE_PROFIT_POLICY = "gtt_ioc_dust_v1"


def submit_take_profit(account, slot, quantity, market, now, settings):
    """Accept an exit intent even when its limit already crosses the bid."""
    price = dec(slot["tp_price"])
    rejection = ("below_min_quantity" if quantity < dec(market["min_qty"]) else
                 "below_min_notional" if quantity * price < dec(market.get("min_notional", "0")) else None)
    dust_ioc = bool(rejection and market.get("take_profit_policy") == TAKE_PROFIT_POLICY)
    if rejection and not dust_ioc:
        return rejection  # Unversioned journal frames retain their original behavior.
    if quantity <= 0:
        return "invalid_quantity"
    if quantity % dec(market["size_step"]):
        return "quantity_off_step"
    # Lighter exempts IOC, not reduce-only GTT, from min base/quote amounts:
    # lighter-prover 28ae613d9c264192e7a36b42f15dda5df3f7103f,
    # circuit/src/types/tx_state.rs:191-246. The original TP limit still applies.
    account["orders"].append({"id": account["next_order"], "slot": slot["slot"], "side": "sell",
                              "price": str(price), "remaining": str(quantity), "queue": None,
                              "submitted_ts": now, "active_ts": now + settings.maker_latency_ms / 1000,
                              "cancel_ts": None, "time_in_force": "IOC" if dust_ioc else "GTT",
                              "activation_pending": True})
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
    """Consume one shared book at arrival; IOC expires, GTT remainder rests."""
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
    fills, expired = [], set()
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
            if order.get("time_in_force") == "IOC":
                fill["time_in_force"] = "IOC"
            fills.append(fill)
            level[1] -= quantity
            order["remaining"] = str(dec(order["remaining"]) - quantity)
            slot["qty"] = str(dec(slot["qty"]) - quantity)
        if order.get("time_in_force") == "IOC":
            # Unfilled quantity stays in the slot, never in a resting IOC order.
            # The caller can submit a fresh attempt, with a new arrival delay.
            expired.add(order["id"])
            continue
        order["activation_pending"] = False
        # No history from this observation can execute newly resting quantity.
        order["active_ts"] = now
        order["rested_ts"] = now
        order["queue"] = (str(sum((qty for price, qty in asks if price == limit), D(0))
                              * dec(config.settings.queue_multiplier)) if limit <= asks[-1][0] else None)
    state["last_tp_book_ts"] = source
    account["orders"] = [o for o in account["orders"] if dec(o["remaining"]) > 0 and o["id"] not in expired]
    return fills


def take_profit_coverage(account, *, include_pending=False):
    """Report exact uncovered inventory, including exchange minimum failures."""
    uncovered = []
    from .qqq_scalper import exit_quantities
    quantities = exit_quantities(account)
    for slot in account["slots"]:
        if (slot["entry_pending"] and not include_pending) or not dec(slot["qty"]):
            continue
        covered = quantities.get(slot["slot"], D(0))
        if dec(slot["qty"]) > covered:
            uncovered.append({"slot": slot["slot"], "quantity": str(dec(slot["qty"]) - covered),
                              "reason": slot.get("tp_rejection", "awaiting_submission")})
    return uncovered
