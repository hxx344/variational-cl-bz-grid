"""Deterministic CPU benchmark; no credentials, network, or persistent ledger."""
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from variational_grid.qqq_hedge import QQQConfig, QQQSettings, initial_account, maker_step
from variational_grid.qqq_scalper import ScalperSettings


def run(repetitions=100):
    results = []
    for count in (3, 30, 200):
        config = QQQConfig(QQQSettings(grid_count=count), "benchmark", "0.05", None, "unused", "3000",
                           scalper=ScalperSettings(model="perp_dex_scalper_v3"), take_profit_percent="0.05")
        account = initial_account()
        for index in range(count):
            account["slots"].append(dict(slot=index+1, entry_price="100", tp_price="100.05", capacity="10",
                                         entered="10", qty="10", entry_pending=False, created_ts=100, next_reprice_ts=120))
            account["orders"].append(dict(id=index+1, slot=index+1, side="sell", price="100.05", remaining="10",
                                          queue="1", active_ts=100, cancel_ts=None))
        account["next_order"] = count+1
        account["qqq"]["qty"] = str(count*10)
        account["scalper"] = dict(last_entry_ts=100, last_close_count=count, next_batch=count+1)
        # Unversioned input permits an identical before/after CPU comparison.
        market = dict(ts=200, bid="99.99", ask="100.01", mark="100", price_tick=".01", size_step=".0001",
                      min_qty=".0075", min_notional="10", bids=[["99.99", "1"]], asks=[["100.01", "1"]],
                      ready=True, gap=False, trades=[])
        timings = []
        for _ in range(repetitions):
            start = time.perf_counter()
            maker_step(account, market, 200, config, False)
            timings.append((time.perf_counter()-start)*1000)
        results.append(dict(batches=count, repetitions=repetitions, median_ms=round(statistics.median(timings), 3)))
    return {"kind": "synthetic_cpu_benchmark", "live_enabled": False, "results": results}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
