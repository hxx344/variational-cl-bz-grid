"""Small, read-only workbench summary of the last published paper sample."""
from contextlib import closing
import json
import math
import sqlite3
import time

from .models import utc
from .reset import control_lock, read_state


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def read_summary(experiment, now=None):
    now = time.time() if now is None else now
    health = {"state": "partial", "message": "模拟实验等待首份采样", "staleAfterSeconds": 60}
    data = {"updatedAt": None, "health": health, "metrics": []}
    result = {"schemaVersion": 2, "data": data}
    with control_lock(experiment):
        reset = read_state(experiment)
        if reset and reset["status"] in {"archiving", "clearing"}:
            health["message"] = "模拟实验正在重置"
            return result
        database = experiment.output / "comparison.sqlite3"
        if not database.is_file():
            return result
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
            runtime = json.loads(row[0]) if row else {}
            row = db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
            if not row:
                return result
            # QQQ stores compressed full samples; no history or individual ledger reads.
            from .qqq_comparison import decode_summary
            sample = decode_summary(row[0])

    if not isinstance(runtime, dict) or not isinstance(sample, dict):
        raise ValueError("Invalid published sample")

    modes = {"paper_comparison": "CL/BZ 价差网格",
             "inventory_comparison": "CL/BZ 库存组合", "qqq_hedge_comparison": "QQQ / US100 对冲"}
    mode = sample.get("mode")
    scenarios = sample.get("scenarios", [])
    if not isinstance(scenarios, list) or any(not isinstance(row, dict) for row in scenarios):
        raise ValueError("Invalid published scenarios")
    poll = number(sample.get("poll_seconds"))
    ttl = min(86400, max(60, math.ceil((poll or 10) * 3)))
    health["staleAfterSeconds"] = ttl
    timestamp = number(sample.get("ts"))
    valid_time = timestamp is not None and 0 < timestamp <= now + 60
    incomplete = not scenarios or mode not in modes or not valid_time
    synthetic = sample.get("data_kind") == "synthetic"
    data["metrics"] = [
        {"key": "mode", "label": "模拟策略", "value": modes.get(mode, "未知模式")},
        {"key": "scenarios", "label": "独立模拟组数", "value": len(scenarios), "unit": "组"},
        {"key": "samples", "label": "本轮采样数", "value": number(sample.get("sample_count")), "unit": "次"},
    ]
    for index, row in enumerate(scenarios[:21]):
        value = number(row.get("total_pnl_usdc"))
        if value is None:
            incomplete = True
        data["metrics"].append({"key": f"paper_pnl_{index + 1}",
            "label": (str(row.get("name") or f"组 {index + 1}")[:60] + " · 模拟盈亏"),
            "value": value, "unit": "USDC",
            "detail": "本轮累计；各组独立，不相加、不计入真实资产；未计资金费" + ("、隔夜费及股息调整" if mode == "qqq_hedge_comparison" else "")})
    incomplete |= len(scenarios) > 21
    if mode == "qqq_hedge_comparison":
        market = sample.get("market") or {}
        if not isinstance(market, dict):
            raise ValueError("Invalid published market")
        source_times = [number(market.get("qqq_source_ts"))]
        # A new QQQ tick must not make an old US100 position valuation look current.
        for row in scenarios:
            leg = row.get("us100") or {}
            if not isinstance(leg, dict):
                raise ValueError("Invalid published position")
            qty = number(leg.get("qty"))
            if qty != 0:
                source_times.append(number(row.get("var_valued_at")))
        for source_time in source_times:
            if source_time is None or not 0 < source_time <= now + 60:
                valid_time = False
                incomplete = True
            elif timestamp is not None:
                timestamp = min(timestamp, source_time)
        incomplete |= bool(market.get("gap")) or market.get("source_status") != "ready"
    if valid_time:
        data["updatedAt"] = utc(timestamp)
    status = runtime.get("status")
    if not valid_time:
        health.update(state="partial", message="模拟采样缺少有效来源时间")
    elif now - timestamp > ttl:
        health.update(state="stale", message="模拟采样已过期，保留末次结果")
    elif status == "stopped":
        health.update(state="offline", message="模拟进程已停止，保留末次结果")
    elif status != "running" or incomplete:
        health.update(state="partial", message="模拟采样暂停或数据不完整，详见模块页面")
    else:
        health.update(state="online", message="模拟采样正常；各组结果独立，不计入真实资产")
    if synthetic:
        health["message"] = "合成行情演示 · " + health["message"]
    return result
