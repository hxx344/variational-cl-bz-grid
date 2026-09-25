"""Paper state machine for sequential near-book entries and independent exits.

Strategy reference: your-quantguy/perp-dex-tools, commit 4679a339b8cdc9998707feeda3c5d8b84fb8681f.
Entries use the public-trade Maker model; v3 exits model GTT arrival and resting quantity.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP
import json

from .models import D, GridError, dec


SOURCE_COMMIT = "4679a339b8cdc9998707feeda3c5d8b84fb8681f"
CURRENT_MODEL = "perp_dex_scalper_v3"
SAFETY_POLICY = "partial_tp_cancel_v1"


@dataclass(frozen=True)
class ScalperSettings:
    model: str = "perp_dex_scalper_v1"
    wait_seconds: float = 450
    reprice_after_seconds: float = 20
    reprice_poll_seconds: float = 5

    @property
    def entry_distance_enabled(self):
        return self.model == "perp_dex_scalper_v1"

    @property
    def gtt_take_profit(self):
        return self.model == "perp_dex_scalper_v3"

    def validate(self):
        if self.model not in {"perp_dex_scalper_v1", "perp_dex_scalper_v2", CURRENT_MODEL}:
            raise GridError("Unknown QQQ scalper model")
        for name in ("wait_seconds", "reprice_after_seconds", "reprice_poll_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < dec(value) <= 86400:
                raise GridError("Invalid QQQ scalper timing")
        return self


def cooldown_seconds(count, maximum, base):
    if count * 3 >= maximum * 2:
        return base * 2
    if count * 3 >= maximum:
        return base
    if count * 6 >= maximum:
        return base / 2
    return base / 4


def candidate_prices(market, closes, config):
    """Preserve the reference price rule, with a strict Maker-only tick cap."""
    from .qqq_hedge import floor
    tick, bid, ask = dec(market["price_tick"]), dec(market["bid"]), dec(market["ask"])
    price = min([(bid + ask) / 2, *(p - tick for p in closes)])
    entry = min((price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick, floor(ask - tick, tick))
    profit = dec(config.take_profit_percent or config.grid_step_percent) / 100
    target = floor(entry * (1 + profit), tick)
    gate = not config.scalper.entry_distance_enabled or not closes or min(closes) / (ask * (1 + profit)) > 1 + dec(config.grid_step_percent) / 100
    return entry, target, gate


def _consume(account, market, now, config):
    from .qqq_hedge import book_fill, floor
    slots = {s["slot"]: s for s in account["slots"]}
    fills = []
    ordered = {side: sorted((o for o in account["orders"] if o["side"] == side),
                           key=lambda o: ((-1 if side == "buy" else 1) * dec(o["price"]), o["id"]))
               for side in ("buy", "sell")}
    for trade in market["trades"]:
        side = "buy" if trade["side"] == "sell" else "sell"
        price, available = dec(trade["price"]), dec(trade["qty"])
        orders = ordered[side]
        for order in orders:
            if (not available or order.get("activation_pending") or order["queue"] is None or trade["ts"] < order["active_ts"]
                    or "rested_ts" in order and trade["ts"] <= order["rested_ts"]
                    or order["cancel_ts"] is not None and trade["ts"] >= order["cancel_ts"]):
                continue
            limit = dec(order["price"])
            if side == "buy" and price > limit or side == "sell" and price < limit:
                continue
            ahead = min(available, dec(order["queue"]) if price == limit else D(0))
            order["queue"] = str((dec(order["queue"]) if price == limit else D(0)) - ahead)
            available -= ahead
            slot = slots[order["slot"]]
            quantity = min(available, dec(order["remaining"]))
            if side == "sell":
                quantity = min(quantity, dec(slot["qty"]))
            quantity = floor(quantity, market["size_step"])
            if not quantity:
                continue
            order["remaining"] = str(dec(order["remaining"]) - quantity)
            available -= quantity
            change = quantity if side == "buy" else -quantity
            fill = book_fill(account, "qqq", change, limit, config.settings.lighter_fee_bps, trade["ts"],
                             "maker_entry" if side == "buy" else "maker_take_profit", slot["slot"])
            fill["maker_model"] = config.scalper.model
            if config.scalper.gtt_take_profit:
                fill["liquidity"] = "maker"
            fills.append(fill)
            slot["qty"] = str(dec(slot["qty"]) + change)
            if side == "buy":
                slot["entered"] = str(dec(slot["entered"]) + quantity)
    account["orders"] = [o for o in account["orders"] if dec(o["remaining"]) > 0
                         and (o["cancel_ts"] is None or o["cancel_ts"] > now)]
    return fills


def _finish_entries(account, now):
    buys = {o["slot"] for o in account["orders"] if o["side"] == "buy"}
    for slot in account["slots"]:
        if slot["entry_pending"] and slot["slot"] not in buys:
            slot["entry_pending"] = False
            if dec(slot["entered"]):
                account["scalper"]["last_entry_ts"] = now
    active = {o["slot"] for o in account["orders"]}
    account["slots"] = [s for s in account["slots"] if s["entry_pending"] or dec(s["qty"]) or s["slot"] in active]


def scalper_step(before, market, now, config, allow_entries):
    from .qqq_hedge import _order, encoded, floor
    account = json.loads(encoded(before))
    state = account.setdefault("scalper", {"last_entry_ts": None, "last_close_count": 0, "next_batch": 1})
    settings, policy = config.settings, config.scalper
    if market["gap"] or not market["ready"]:
        account["orders"] = []
        account["gap_count"] += int(market["gap"])
        _finish_entries(account, now)
        state["status"] = _status(account, market, now, config, "market_gap", None)
        return account, []

    fills = _consume(account, market, now, config)
    _finish_entries(account, now)
    safety = policy.gtt_take_profit and market.get("scalper_safety_policy") == SAFETY_POLICY
    stale = now - market["ts"] > settings.max_quote_age_seconds
    if safety and (not allow_entries or stale):
        for order in account["orders"]:
            if order["side"] == "buy" and order["cancel_ts"] is None:
                order["cancel_ts"] = now + settings.cancel_latency_ms / 1000
    if safety and stale:
        state["status"] = _status(account, market, now, config, "market_gap", None)
        return account, fills
    # Unknown depth gets a fresh queue anchor, never a retroactive fill.
    for order in account["orders"]:
        if order["queue"] is None and not order.get("activation_pending"):
            price = dec(order["price"])
            depth = market["bids" if order["side"] == "buy" else "asks"]
            covered = depth and (price >= min(dec(p) for p, _ in depth) if order["side"] == "buy" else price <= max(dec(p) for p, _ in depth))
            if covered:
                order["queue"] = str(sum((dec(q) for p, q in depth if dec(p) == price), D(0)) * dec(settings.queue_multiplier))
                order["active_ts"] = now + settings.maker_latency_ms / 1000

    if now - market["ts"] > settings.max_quote_age_seconds:
        state["status"] = _status(account, market, now, config, "market_gap", None)
        return account, fills
    if policy.gtt_take_profit:
        from .qqq_execution import activate_take_profits
        fills.extend(activate_take_profits(account, market, now, config))
        _finish_entries(account, now)
    closes = [dec(s["tp_price"]) for s in account["slots"] if not s["entry_pending"] and dec(s["qty"])]
    entry, target, gate = candidate_prices(market, closes, config)
    opening = [o for o in account["orders"] if o["side"] == "buy"]
    if len(opening) > 1:
        raise GridError("Scalper permits only one pending entry")
    if opening:
        order = opening[0]
        slot = next(s for s in account["slots"] if s["slot"] == order["slot"])
        if order["cancel_ts"] is None:
            if not allow_entries:
                order["cancel_ts"] = now + settings.cancel_latency_ms / 1000
            elif now >= slot["next_reprice_ts"]:
                slot["next_reprice_ts"] = now + policy.reprice_poll_seconds
                if entry > dec(order["price"]):
                    order["cancel_ts"] = now + settings.cancel_latency_ms / 1000
        phase = "cancel_pending" if order["cancel_ts"] is not None else "awaiting_fill"
    else:
        phase = "entry_paused"

    # New observations also protect filled quantity while the entry remains open.
    # Unversioned frames keep the reference's wait-for-entry-completion behavior.
    covered_by_slot = exit_quantities(account)
    for slot in account["slots"]:
        if (slot["entry_pending"] and not safety) or not dec(slot["qty"]):
            continue
        covered = covered_by_slot.get(slot["slot"], D(0))
        if dec(slot["qty"]) > covered:
            if policy.gtt_take_profit:
                from .qqq_execution import submit_take_profit
                slot["tp_rejection"] = submit_take_profit(account, slot, dec(slot["qty"]) - covered, market, now, settings)
            else:
                _order(account, slot, "sell", dec(slot["qty"]) - covered, dec(slot["tp_price"]), market, now, settings)

    waived = False
    if not opening and allow_entries:
        exit_slots = {o["slot"] for o in account["orders"] if o["side"] == "sell"}
        count = len(account["slots"])
        wait = cooldown_seconds(count, settings.grid_count, policy.wait_seconds)
        remaining = 0 if state["last_entry_ts"] is None else state["last_entry_ts"] + wait - now
        # Like the reference, only a net decline since the last entry decision
        # waives this round's cooldown; in v1 a failed price gate consumes the waiver.
        credit = count < state["last_close_count"]
        waived = credit
        state["last_close_count"] = count
        if count >= settings.grid_count:
            phase = "capacity_full"
        elif policy.gtt_take_profit and any(s.get("tp_rejection") for s in account["slots"]):
            phase = "take_profit_pending"
        elif not credit and state["last_entry_ts"] is not None and remaining >= 0:
            phase = "cooling_down"
        elif not gate:
            phase = "grid_blocked"
        elif any(s["slot"] not in exit_slots for s in account["slots"]):
            phase = "take_profit_pending"
        elif entry <= 0 or target <= entry:
            phase = "post_only_wait"
        else:
            size = floor(dec(settings.order_notional_usdc) / entry, market["size_step"])
            slot = {"slot": state["next_batch"], "entry_price": str(entry), "tp_price": str(target),
                    "capacity": str(size), "entered": "0", "qty": "0", "entry_pending": True,
                    "created_ts": now, "next_reprice_ts": now + policy.reprice_after_seconds}
            _order(account, slot, "buy", size, entry, market, now, settings)
            if any(o["slot"] == slot["slot"] and o["side"] == "buy" for o in account["orders"]):
                account["slots"].append(slot)
                state["next_batch"] += 1
                phase = "opening"
            else:
                phase = "post_only_wait"
    if dec(account["qqq"]["qty"]) != sum((dec(s["qty"]) for s in account["slots"]), D(0)) or len(account["slots"]) > settings.grid_count:
        raise GridError("Scalper batch and inventory accounting differ")
    if policy.gtt_take_profit:
        covered_by_slot = exit_quantities(account)
        for slot in account["slots"]:
            covered = covered_by_slot.get(slot["slot"], D(0))
            if not 0 <= covered <= dec(slot["qty"]):
                raise GridError("Take-profit orders exceed held inventory")
    state["status"] = _status(account, market, now, config, phase, gate, waived)
    return account, fills


def exit_quantities(account):
    covered = {}
    for order in account["orders"]:
        if order["side"] == "sell":
            covered[order["slot"]] = covered.get(order["slot"], D(0)) + dec(order["remaining"])
    return covered


def _status(account, market, now, config, phase, gate, waived=False):
    state = account["scalper"]
    closes = [dec(s["tp_price"]) for s in account["slots"] if not s["entry_pending"] and dec(s["qty"])]
    entry, target, _ = candidate_prices(market, closes, config)
    wait = cooldown_seconds(len(closes), config.settings.grid_count, config.scalper.wait_seconds)
    next_at = None if state["last_entry_ts"] is None else state["last_entry_ts"] + wait
    result = {"model": config.scalper.model, "phase": phase, "wait_seconds": config.scalper.wait_seconds,
            "cooldown_waived": waived,
            "cooldown_seconds": wait, "cooldown_remaining_seconds": 0 if next_at is None else max(0, next_at - now),
            "next_entry_at": next_at, "grid_allowed": gate, "candidate_entry_price": str(entry), "candidate_tp_price": str(target),
            "active_entries": sum(o["side"] == "buy" for o in account["orders"]),
            "active_take_profits": sum(o["side"] == "sell" for o in account["orders"]),
            "occupied_batches": len(account["slots"]), "max_batches": config.settings.grid_count,
            "take_profit_percent": config.take_profit_percent or config.grid_step_percent}
    if not config.scalper.entry_distance_enabled:
        result.update(entry_distance_enabled=False, grid_allowed=None)
    if config.scalper.gtt_take_profit:
        from .qqq_execution import TAKE_PROFIT_POLICY, take_profit_coverage
        safety = market.get("scalper_safety_policy") == SAFETY_POLICY
        result.update(take_profit_execution="gtt_limit_v1", take_profit_blockers=take_profit_coverage(account, include_pending=safety),
                      take_profits_in_flight=sum(bool(o.get("activation_pending")) for o in account["orders"]))
        if safety:
            result["partial_entry_protection"] = SAFETY_POLICY
            covered = exit_quantities(account)
            result["partial_entries"] = [{"slot": s["slot"], "quantity": s["qty"],
                                          "take_profit_submitted_quantity": str(covered.get(s["slot"], D(0)))}
                                         for s in account["slots"] if s["entry_pending"] and dec(s["qty"]) > 0]
        if market.get("take_profit_policy") == TAKE_PROFIT_POLICY:
            result.update(take_profit_execution=TAKE_PROFIT_POLICY,
                          small_take_profits=[{"slot": o["slot"], "quantity": o["remaining"], "limit": o["price"]}
                                             for o in account["orders"] if o.get("time_in_force") == "IOC"])
    return result
