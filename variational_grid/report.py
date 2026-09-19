"""Self-contained, read-only comparison view. No credentials or external assets."""
from datetime import datetime, timezone
from html import escape

from .models import dec


def number(value, digits=3):
    return f"{dec(value):,.{digits}f}"


def chart(name, recent, low, high, poll, width=680):
    points = [(s["ts"], float(next(r for r in s["scenarios"] if r["name"] == name)["total_pnl_usdc"])) for s in recent]
    if not points:
        return '<p class="empty">等待有效采样</p>'
    start, end = points[0][0], points[-1][0]
    height, right = 200, width - 28
    def x(t):
        return 60 + (t - start) / max(end - start, 1) * (right - 60)
    def y(v):
        return 160 - (v - low) / (high - low) * 140
    ticks = [low, (low + high) / 2, high]
    elements = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(name)} 总盈亏曲线，单位 USDC"><title>{escape(name)}，最后盈亏 {points[-1][1]:.3f} USDC</title>']
    for value in ticks:
        elements.append(f'<line x1="60" x2="{right}" y1="{y(value):.2f}" y2="{y(value):.2f}" class="gridline"/><text x="52" y="{y(value)+4:.2f}" text-anchor="end">{value:.3f}</text>')
    if low <= 0 <= high:
        elements.append(f'<line x1="60" x2="{right}" y1="{y(0):.2f}" y2="{y(0):.2f}" class="zero"/>')
    commands = []
    previous = None
    for t, value in points:
        move = previous is None or t - previous > max(60, poll * 3)
        commands.append(f'{"M" if move else "L"}{x(t):.2f},{y(value):.2f}')
        previous = t
    elements.append(f'<path d="{" ".join(commands)}" fill="none" stroke="#2563a6" stroke-width="2"/>')
    elements.append(f'<circle cx="{x(end):.2f}" cy="{y(points[-1][1]):.2f}" r="3" fill="#2563a6"/>')
    labels = ((start, "start"), (end, "end")) if start != end else ((start, "start"),)
    for t, anchor in labels:
        label = datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M:%S")
        elements.append(f'<text x="{x(t):.2f}" y="188" text-anchor="{anchor}">{label}</text>')
    return "".join(elements) + "</svg>"


def render_report(summary, recent, runtime):
    status_labels = {"running": "模拟运行中", "paused": "行情暂停", "stopped": "已停止", "starting": "等待行情"}
    label = status_labels.get(runtime["status"], "状态未知")
    ts = summary["ts"] if summary else 0
    poll = summary["poll_seconds"] if summary else 10
    if not summary:
        content = '<p class="empty">还没有完整有效的共同报价。所有配置会从同一份有效行情开始。</p>'
        sample = "0 次共同采样"
    else:
        rows = summary["scenarios"]
        sample = f'{summary["sample_count"]:,} 次共同采样 · 开始 {escape(summary["started_utc"])}'
        spread = rows[0]["spread_bz_minus_cl"]
        content = f'<section class="market"><div><span>当前 BZ − CL</span><strong>{number(spread,4)}<small> USDC/桶</small></strong></div><div><span>7 日中枢</span><strong>{number(summary["center_7d"],4)}<small> USDC/桶</small></strong></div><div><span>最新采样 · UTC</span><strong class="date">{escape(summary["time_utc"])}</strong></div></section>'
        content += '<h2>参数与结果</h2><p>各组资金独立，使用相同采样时刻；总盈亏包含未平仓持仓的退出估值。尚不足以判断长期优劣。</p><div class="table-wrap" tabindex="0" role="region" aria-label="网格配置对照表，可横向滚动"><table><thead><tr><th>配置</th><th>间距 / 净止盈<br>USDC/桶</th><th>每腿桶数<br>最多组数</th><th>初始资金<br>USDC</th><th>总盈亏<br>USDC</th><th>已实现<br>USDC</th><th>最大回撤</th><th>已平 / 已开组</th><th>当前持仓组</th><th>保证金 / 峰值<br>USDC</th><th>状态</th></tr></thead><tbody>'
        for r in rows:
            state = "回撤停机" if r["halted"] else "只减仓" if not r["open_allowed"] else "正常"
            content += f'<tr><th scope="row">{escape(r["name"])}</th><td>{number(r["grid_step"],2)}</td><td>{number(r["quantity_barrels"])} / {r["max_levels"]}</td><td>{number(r["initial_balance_usdc"],2)}</td><td class="pnl">{number(r["total_pnl_usdc"],4)}</td><td>{number(r["realized_pnl_usdc"],4)}</td><td>{number(dec(r["max_drawdown_fraction"])*100,3)}%</td><td>{r["closed_pairs"]} / {r["opened_pairs"]}</td><td>{r["open_pairs"]}</td><td>{number(r["margin_usdc"],2)} / {number(r["max_margin_usdc"],2)}</td><td>{state}</td></tr>'
        content += '</tbody></table></div><h2>总盈亏走势 <small>USDC · UTC</small></h2><p>最近最多 360 次有效采样，所有图使用相同纵轴；超过三个采样周期（至少 60 秒）的缺口断线显示。</p><div class="charts">'
        values = [float(r["total_pnl_usdc"]) for s in recent for r in s["scenarios"]] or [0]
        low, high = min([0] + values), max([0] + values)
        padding = max((high - low) * .12, .01)
        for r in rows:
            desktop = chart(r["name"],recent,low-padding,high+padding,poll)
            mobile = chart(r["name"],recent,low-padding,high+padding,poll,360)
            content += f'<article><h3>{escape(r["name"])} <span>{number(r["total_pnl_usdc"],4)} USDC</span></h3><div class="desktop-chart">{desktop}</div><div class="mobile-chart">{mobile}</div></article>'
        content += '</div><details><summary>成本假设与数据口径</summary><ul>'
        for r in rows:
            content += f'<li>{escape(r["name"])}：每腿每次手续费 {number(r["fee_bps"],2)} bp，滑点 {number(r["slippage_bps"],2)} bp；买入用 ask，卖出用 bid。</li>'
        content += f'</ul><p>中枢：{escape(summary["history_start_utc"])} 至 {escape(summary["history_end_utc"])}，168 根对齐、已收盘小时 K 线。最大回撤和峰值保证金从本次实验首个有效采样起累计。</p></details>'
    reason = '<p class="reason">' + escape(runtime["reason"]) + '</p>' if runtime.get("reason") else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CL / BZ · 网格对照模拟</title><meta http-equiv="refresh" content="10"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#172533;font:15px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}}main{{max-width:1450px;margin:auto;padding:32px 28px}}header{{border-bottom:2px solid #253b51;padding-bottom:22px}}.eyebrow{{letter-spacing:.16em;font-size:12px;color:#425a71}}h1{{font-size:30px;margin:8px 0}}h2{{font-size:21px;margin:28px 0 8px}}h3{{font-size:16px;margin:0 0 12px;display:flex;justify-content:space-between;gap:12px}}p{{color:#4b5d6e}}small{{font-size:12px;font-weight:400}}.status{{display:inline-block;border:1px solid #8ba2b5;border-radius:5px;padding:4px 10px;background:#fff}}.muted{{font-size:13px;color:#4b5d6e}}.reason{{padding:10px;border-left:3px solid #ad651c;background:#fff5e5}}.market{{display:flex;gap:44px;flex-wrap:wrap;margin:26px 0}}.market span{{display:block;color:#4b5d6e;font-size:13px}}strong{{display:block;font-size:27px;font-variant-numeric:tabular-nums}}.date{{font-size:15px;padding-top:10px}}.table-wrap{{overflow:auto;background:#fff;border:1px solid #d6dfe7;border-radius:8px}}table{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}}th,td{{padding:14px 13px;text-align:right;border-bottom:1px solid #e4eaf0;font-variant-numeric:tabular-nums}}th:first-child{{text-align:left}}thead th{{font-weight:500;color:#425a71;background:#edf2f6}}.pnl{{font-weight:700}}.charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}article{{background:#fff;border:1px solid #d6dfe7;border-radius:8px;padding:18px}}svg{{width:100%;height:auto;display:block}}svg text{{font:11px system-ui;fill:#4b5d6e}}.gridline{{stroke:#e3e9ee}}.zero{{stroke:#8396a7;stroke-dasharray:4 4}}details{{margin-top:24px}}summary{{cursor:pointer;padding:10px 0}}footer{{border-top:1px solid #cad5df;margin-top:26px;padding-top:16px;color:#536575;font-size:13px}}.empty{{padding:40px 0}}@media(max-width:700px){{main{{padding:22px 14px}}h1{{font-size:25px}}.market{{gap:20px}}.charts{{grid-template-columns:1fr}}article{{padding:12px}}.table-wrap:focus{{outline:2px solid #2563a6}}}}
.mobile-chart{{display:none}}@media(max-width:700px){{.desktop-chart{{display:none}}.mobile-chart{{display:block}}.mobile-chart svg text{{font-size:12px}}}}
</style></head><body><main><header><div class="eyebrow">VARIATIONAL · CL / BZ</div><h1>不同间距，同一段行情</h1><span class="status" id="status">{escape(label)}</span><p class="muted">{sample}</p><p class="muted" id="age"></p>{reason}</header>{content}<footer>仅模拟成交 · 未计实际资金费和强平机制 · 不保证真实双腿成交价。页面每 10 秒重载；行情中断时保留上次数据并标明过期。</footer></main><script>
const last={ts}, threshold={max(60,poll*3)}, running={str(runtime['status']=='running').lower()};
function freshness(){{const age=last ? Math.max(0,Math.floor(Date.now()/1000-last)) : null;document.getElementById('age').textContent=age===null?'等待首次采样':`最后有效行情距今 ${{age}} 秒`;if(age!==null && age>threshold){{document.getElementById('status').textContent=running?'行情已过期':'{escape(label)} · 数据已过期';}}}}freshness();setInterval(freshness,1000);
</script></body></html>'''
