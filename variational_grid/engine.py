"""Equal-barrel BZ-CL grid. Positive direction means long BZ / short CL."""
import json

from .models import D, GridError, HOUR, dec, utc, validate_pair


class Engine:
    def __init__(self, config, store):
        self.config = config.validate()
        self.store = store
        self.qty = dec(config.quantity_barrels)
        self.slip = dec(config.slippage_bps_per_leg) / 10000
        self.fee = dec(config.fee_bps_per_leg) / 10000

    def price(self, quote, side):
        return quote.ask * (1 + self.slip) if side == "buy" else quote.bid * (1 - self.slip)

    def execution(self, direction, cl, bz, opening):
        bz_side = "buy" if (direction == 1) == opening else "sell"
        cl_side = "sell" if bz_side == "buy" else "buy"
        return [(cl, cl_side, self.price(cl, cl_side)), (bz, bz_side, self.price(bz, bz_side))]

    def exit_value(self, lot, cl, bz):
        fills = self.execution(lot["direction"], cl, bz, False)
        cl_exit, bz_exit = fills[0][2], fills[1][2]
        qty = dec(lot["qty"])
        gross = lot["direction"] * qty * ((bz_exit - dec(lot["entry_bz"])) - (cl_exit - dec(lot["entry_cl"])))
        exit_fee = qty * (cl_exit + bz_exit) * self.fee
        return gross - exit_fee, fills

    def equity(self, cl, bz):
        # Entry fees have already been debited from cash. Value inventory at liquidation prices.
        return dec(self.store.get("cash")) + sum((self.exit_value(lot, cl, bz)[0] for lot in self.store.lots()), D(0))

    def record_fills(self, lot_id, now, phase, fills):
        for quote, side, price in fills:
            self.store.db.execute("INSERT INTO fills(lot_id,ts,phase,symbol,side,qty,price,fee) VALUES (?,?,?,?,?,?,?,?)",
                                  (lot_id, now, phase, quote.symbol, side, str(self.qty), str(price), str(self.qty * price * self.fee)))
            self.store.set("volume_barrels", dec(self.store.get("volume_barrels")) + self.qty)
            self.store.set("turnover_usdc", dec(self.store.get("turnover_usdc")) + self.qty * price)
            self.store.set("fill_count", int(self.store.get("fill_count")) + 1)

    def open(self, direction, level, center, cl, bz, now):
        fills = self.execution(direction, cl, bz, True)
        cl_price, bz_price = fills[0][2], fills[1][2]
        fee = self.qty * (cl_price + bz_price) * self.fee
        cur = self.store.db.execute("INSERT INTO lots(direction,level,qty,entry_center,entry_cl,entry_bz,entry_fee,opened) VALUES (?,?,?,?,?,?,?,?)",
                                    (direction, level, str(self.qty), str(center), str(cl_price), str(bz_price), str(fee), now))
        self.record_fills(cur.lastrowid, now, "open", fills)
        self.store.set("cash", dec(self.store.get("cash")) - fee)
        self.store.set("fees", dec(self.store.get("fees")) + fee)
        self.store.event(now, "open", f"lot={cur.lastrowid}, direction={direction}, level={level}")
        return cur.lastrowid

    def close(self, lot, cl, bz, now, reason):
        value, fills = self.exit_value(lot, cl, bz)
        pnl = value - dec(lot["entry_fee"])
        self.record_fills(lot["id"], now, "close", fills)
        self.store.db.execute("UPDATE lots SET closed=?,exit_cl=?,exit_bz=?,net_pnl=?,reason=? WHERE id=?",
                              (now, str(fills[0][2]), str(fills[1][2]), str(pnl), reason, lot["id"]))
        self.store.set("cash", dec(self.store.get("cash")) + value)
        self.store.set("fees", dec(self.store.get("fees")) + sum((self.qty * price * self.fee for _, _, price in fills), D(0)))
        self.store.set("realized", dec(self.store.get("realized")) + pnl)
        self.store.event(now, "close", f"lot={lot['id']}, reason={reason}, net_pnl={pnl}")

    def tick(self, center, cl, bz, now, *, allow_open=True):
        validate_pair(cl, bz, now, self.config)
        center = dec(center)
        spread = bz.mark - cl.mark
        deviation = spread - center
        direction = -1 if deviation > 0 else 1
        step = self.config.grid_step(center)
        depth = min(int(abs(deviation) / step), self.config.max_levels) if step else 0
        actions = []
        with self.store.transaction():
            previous = self.store.get("last_tick")
            if previous is not None and now <= float(previous):
                raise GridError("Tick time must advance; repeated or out-of-order tick rejected")
            peak = max(dec(self.store.get("peak")), self.equity(cl, bz))
            self.store.set("peak", peak)
            drawdown = (peak - self.equity(cl, bz)) / peak
            if drawdown >= dec(self.config.max_drawdown_fraction):
                self.store.set("halted", "max_drawdown")
            blocked = {tuple(x) for x in json.loads(self.store.get("blocked"))}
            # A closed slot must return inside its threshold before it can be entered again.
            blocked = {(d, level) for d, level in blocked if d == direction and depth >= level}
            for lot in self.store.lots():
                net = self.exit_value(lot, cl, bz)[0] - dec(lot["entry_fee"])
                reason = self.store.get("halted")
                if not reason and now - lot["opened"] >= self.config.max_holding_hours * HOUR:
                    reason = "max_holding"
                # Freeze each lot's target at its entry center; a moving center must not reprice it.
                if not reason and net >= dec(lot["qty"]) * self.config.grid_step(lot["entry_center"]):
                    reason = "take_profit"
                if reason:
                    self.close(lot, cl, bz, now, reason)
                    actions.append({"action": "close", "lot_id": lot["id"], "reason": reason})
                    blocked.add((lot["direction"], lot["level"]))
            lots = self.store.lots()
            equity = self.equity(cl, bz)
            margin_one = self.qty * (cl.mark + bz.mark) / dec(self.config.paper_leverage)
            margin = margin_one * len(lots)
            skip_reason = "zero_center" if not step else None
            if allow_open and not actions and not self.store.get("halted") and depth:
                used = {(lot["direction"], lot["level"]) for lot in lots}
                levels = [level for level in range(1, depth + 1) if (direction, level) not in used | blocked]
                if any(lot["direction"] != direction for lot in lots):
                    skip_reason = "opposite_inventory"
                elif levels and len(lots) < self.config.max_levels:
                    entry_fills = self.execution(direction, cl, bz, True)
                    entry_fee = sum((p * self.qty * self.fee for _, _, p in entry_fills), D(0))
                    round_trip_drag = sum((self.qty * (self.price(q, "buy") - self.price(q, "sell")) for q in (cl, bz)), D(0))
                    exit_fees = sum((p * self.qty * self.fee for _, _, p in self.execution(direction, cl, bz, False)), D(0))
                    post_equity = equity - entry_fee - exit_fees - round_trip_drag
                    if post_equity <= 0 or margin + margin_one > min(post_equity, dec(self.config.paper_balance_usdc)) * dec(self.config.max_margin_fraction):
                        skip_reason = "margin_budget"
                    elif (peak - post_equity) / peak >= dec(self.config.max_drawdown_fraction):
                        skip_reason = "entry_drawdown"
                    else:
                        lot_id = self.open(direction, levels[0], center, cl, bz, now)
                        actions.append({"action": "open", "lot_id": lot_id, "direction": direction, "level": levels[0]})
            self.store.set("blocked", json.dumps(sorted(blocked)))
            self.store.set("last_tick", now)
            equity = self.equity(cl, bz)
            lots = self.store.lots()
            position_bz = sum((dec(lot["qty"]) * lot["direction"] for lot in lots), D(0))
            fees = dec(self.store.get("fees"))
            realized = dec(self.store.get("realized"))
            snapshot = {
                "mode": "paper", "time_utc": utc(now), "center_7d": str(center), "spread_bz_minus_cl": str(spread),
                "cl_mark": str(cl.mark), "bz_mark": str(bz.mark), "deviation": str(deviation),
                "grid_step": str(step), "grid_step_percent": self.config.grid_step_percent,
                "grid_step_basis": "center_7d_absolute" if self.config.grid_step_percent is not None else "absolute",
                **self.config.grid_geometry(center),
                "equity_usdc": str(equity), "cash_usdc": self.store.get("cash"), "realized_pnl_usdc": str(realized),
                "total_pnl_usdc": str(equity - dec(self.config.paper_balance_usdc)), "fees_usdc": str(fees),
                "pnl_basis": "before_funding", "open_pairs": len(lots), "cl_barrels": str(-position_bz), "bz_barrels": str(position_bz),
                "margin_usdc": str(margin_one * len(lots)), "drawdown_fraction": str((peak - equity) / peak),
                "halted": self.store.get("halted") or None, "open_allowed": allow_open,
                "skip_reason": skip_reason, "actions": actions, **self.store.volume(),
            }
            self.store.db.execute("INSERT INTO ticks(ts,snapshot) VALUES (?,?)", (now, json.dumps(snapshot)))
            # Bound routine telemetry to seven days; keep every fill, lot and event.
            self.store.db.execute("DELETE FROM ticks WHERE ts < ?", (now - 7 * 24 * HOUR,))
            return snapshot
