/* Decimal ledger values are authoritative; browser arithmetic only draws and checks display. */
(function (root) {
  'use strict';
  const M = typeof module !== 'undefined' && module.exports ? require('./model.js') : root.GridModel;
  const colors = ['#285de5', '#098477', '#a46a0f', '#8051a2', '#a64965'];
  const dashes = ['', '7 3', '2 3', '10 3 2 3', '12 4'];
  const tolerance = row => row?.tolerance_percent === null ? '不限' : M.finite(row?.tolerance_percent) ? Number(row.tolerance_percent) + '%' : '—';
  const percent = (value, digits = 2) => M.finite(value) ? M.number(Number(value) * 100, digits) + '%' : '—';
  function reconciliation(row) {
    const keys = ['direction_pnl_usdc', 'spread_pnl_usdc', 'execution_cost_usdc', 'exit_cost_reserve_usdc', 'total_pnl_usdc'];
    if (!keys.every(k => M.finite(row?.[k]))) return {difference: null, matches: null};
    const [direction, spread, cost, reserve, total] = keys.map(k => Number(row[k]));
    const difference = direction + spread - cost - reserve - total;
    const precision = Math.max(1e-8, Math.max(...keys.map(k => Math.abs(Number(row[k])))) * 1e-10);
    return {difference, matches: Math.abs(difference) <= precision};
  }
  function exposure(row) {
    if (!M.finite(row?.exposure_seconds) || !M.finite(row?.observed_seconds) || Number(row.observed_seconds) <= 0) return null;
    return Number(row.exposure_seconds) / Number(row.observed_seconds);
  }
  function sampleIndex(points, ts) {
    if (!points.length) return -1;
    if (ts === null) return points.length - 1;
    let nearest = 0;
    points.forEach((point, i) => { if (Math.abs(point.ts - ts) < Math.abs(points[nearest].ts - ts)) nearest = i; });
    return nearest;
  }
  function ratioDomain(values, threshold) {
    const valid = values.filter(M.finite).map(Number);
    if (M.finite(threshold)) valid.push(Number(threshold));
    return [0, Math.min(1, Math.max(0.05, ...valid) * 1.15)];
  }
  const helpers = {tolerance, percent, reconciliation, exposure, sampleIndex, ratioDomain};
  if (typeof module !== 'undefined' && module.exports) { module.exports = helpers; return; }
  root.InventoryModel = helpers;
  const $ = id => document.getElementById(id), E = M.escape;
  let data = null, state = M.state(location.search, []), page = 0, selectedTs = null;
  let timer = null, controller = null, disconnected = false, received = 0, serverAge = 0;
  let resetSending = false, resetError = '', resetGeneration = null;
  const allRows = () => data?.summary?.scenarios || [];
  const names = () => allRows().map(r => r.name);
  const rows = () => allRows().filter(r => state.strategy === 'all' || r.name === state.strategy);
  const filtered = values => (values || []).filter(r => state.strategy === 'all' || r.scenario === state.strategy);
  const index = name => Math.max(0, names().indexOf(name));
  const cls = name => 'c' + index(name) % colors.length;
  const label = row => '容忍 ' + tolerance(row);
  const rowLabel = name => label(allRows().find(r => r.name === name));
  const pnl = value => `<span class="${M.tone(value)}">${M.signed(value, 4)}</span>`;
  const reason = value => ({inventory_hedge:'库存纠偏',inventory_control:'库存纠偏',hedge:'库存纠偏',rebalance:'库存纠偏',signal:'策略信号净成交',signal_and_inventory:'策略信号与库存纠偏',scalp_take_profit:'Scalp 止盈',take_profit:'止盈',max_drawdown:'回撤停机',max_holding:'持仓到期',price_grid:'价格网格',spread_grid:'价差网格',scalp:'Scalp',initial:'初始建仓',zero_center:'中枢为零','Venue quantity step prevents exact target':'数量步长限制，保留实际残差'}[value] || value || '—');
  function controlLabel(row) {
    if (row.halted) return '回撤停机';
    return ({neutral:'方向中性',within_tolerance:'容忍范围内',within:'容忍范围内',inside:'容忍范围内',balanced:'方向中性',hedged:'已纠偏',adjusted:'已纠偏',quantity_limited:'存在数量残差',unlimited:'不限方向偏离',uncontrolled:'不限方向偏离',close_only:'仅减仓',disabled:'不限方向偏离',idle:'等待信号',paused:'暂停',active:'控制运行中',ok:'容忍范围内'}[row.control_status] || row.control_status || '等待状态');
  }
  function setState(change, push = true) {
    state = {...state, ...change}; page = 0;
    const query = new URLSearchParams();
    if (state.strategy !== 'all') query.set('strategy', state.strategy);
    query.set('range', state.range); query.set('view', state.view);
    if (push) history.pushState({}, '', '?' + query.toString());
    render();
  }
  function resetStatus() {
    const r = data?.reset, busy = r && ['pending', 'archiving', 'clearing'].includes(r.status);
    $('reset').disabled = !r || busy || resetSending || disconnected;
    let message = resetError;
    if (busy) message = r.status === 'pending' ? '重置请求已保存，等待模拟进程完成当前采样；若进程已停止，需先启动模拟服务。' : '正在归档并重置全部模拟组…';
    else if (r?.status === 'complete') message = '旧账本已归档，当前为新一轮模拟。归档编号 ' + r.archive_id;
    else if (r?.status === 'failed') message = '归档失败，原模拟数据已保留；请检查磁盘空间与服务日志后重试。';
    $('reset-message').textContent = message; $('reset-message').hidden = !message;
  }
  function status() {
    resetStatus();
    if (!data) return;
    const s = data.summary, age = s ? Math.max(0, serverAge + (Date.now() - received) / 1000) : null;
    const old = age !== null && age > Math.max(60, Number(s.poll_seconds) * 3);
    const runtime = data.runtime?.status;
    const labels = {running:'模拟运行中',paused:'行情暂停',stopped:'模拟已停止',starting:'等待行情',resetting:'正在重置'};
    $('status').textContent = disconnected ? '页面连接中断' : old ? '行情已过期' : labels[runtime] || '状态未知';
    $('status').className = 'status' + (disconnected ? ' error' : old || runtime !== 'running' ? ' warn' : '');
    $('freshness').textContent = age === null ? '等待第一份有效行情' : `最近行情 ${M.date(s.ts)} · ${Math.floor(age)} 秒前 · 每 ${M.number(s.poll_seconds, 0)} 秒采样`;
    let notice = '';
    if (disconnected) notice = '无法连接监控服务，保留上次成功读取的数据。页面会自动重试。';
    else if (runtime === 'paused') notice = '行情暂停，保留最近有效采样：' + (data.runtime.reason || '等待数据恢复');
    else if (runtime === 'stopped') notice = '模拟进程已停止，以下为最后保存的数据。';
    else if (old) notice = '行情已过期，收益和仓位估值停留在最后有效采样。';
    else if (s && !data.details_available) notice = '汇总可用，对应仓位或成交明细暂不可用。';
    $('notice').textContent = notice; $('notice').hidden = !notice;
  }
  function renderStrategies() {
    if (!allRows().length) {
      $('strategies').innerHTML = '<div class="empty">暂无有效采样。收到共同市场行情后，将显示各组真实模拟账本。</div>';
      $('experiment-info').textContent = '等待本轮第一份有效行情';
      $('data-kind').textContent = '模拟交易 · 等待行情';
      $('strategy-select').innerHTML = '<option value="all">全部策略</option>';
      return;
    }
    $('strategies').innerHTML = allRows().map(r => `<button class="strategy ${cls(r.name)} ${state.strategy === r.name ? 'selected' : ''}" data-strategy="${E(r.name)}" aria-pressed="${state.strategy === r.name}" aria-label="查看${E(label(r))}"><div class="strategy-head"><h3>${E(label(r))}</h3><small>实际净桶数</small></div><span class="pnl-label">累计总收益</span><strong class="big-pnl ${M.tone(r.total_pnl_usdc)}">${M.signed(r.total_pnl_usdc, 4)}<small>USDC</small></strong><div class="strategy-stats"><div><span>方向偏离</span><b>${percent(r.inventory_ratio)}</b></div><div><span>最大回撤</span><b>${percent(r.max_drawdown_fraction)}</b></div><div><span>CL 净桶数</span><b>${M.signed(r.cl_barrels, 3)}</b></div><div><span>BZ 净桶数</span><b>${M.signed(r.bz_barrels, 3)}</b></div></div><p class="control-status">${E(controlLabel(r))}${r.control_reason ? ' · ' + E(reason(r.control_reason)) : ''}</p></button>`).join('');
    $('strategy-select').innerHTML = '<option value="all">全部策略</option>' + allRows().map(r => `<option value="${E(r.name)}">${E(label(r))}</option>`).join('');
    $('strategy-select').value = state.strategy;
    const s = data.summary;
    $('experiment-info').textContent = `${allRows().length} 组独立账户 · ${M.number(s.sample_count, 0)} 次共同采样 · 开始于 ${M.date(Date.parse(s.started_utc) / 1000)}`;
    $('data-kind').textContent = s.data_kind === 'synthetic' ? '合成行情 · 模拟数据' : s.data_kind === 'live_indicative' ? '实时指示性行情 · 模拟成交' : '模拟交易 · 数据类型未提供';
  }
  function drawChart(id, field, isRatio = false) {
    const host = $(id), points = data?.history?.points || [];
    if (!points.length) {host.innerHTML = '<div class="empty">等待有效历史采样</div>'; return;}
    const selected = rows(), width = Math.max(250, host.clientWidth), height = host.clientHeight || 260;
    const left = isRatio ? 47 : 62, right = width - 14, top = 22, bottom = height - 31;
    const threshold = isRatio && state.strategy !== 'all' && M.finite(selected[0]?.tolerance_percent) ? Number(selected[0].tolerance_percent) / 100 : null;
    const values = points.flatMap(p => selected.map(r => p.scenarios?.[r.name]?.[field]));
    const [lo, hi] = isRatio ? ratioDomain(values, threshold) : M.domain(values, true);
    const first = points[0].ts, last = points.at(-1).ts;
    const x = t => first === last ? (left + right) / 2 : left + (t - first) / (last - first) * (right - left);
    const y = value => bottom - (value - lo) / (hi - lo) * (bottom - top);
    const axisLabel = value => isRatio ? percent(value, 0) : M.number(value, hi - lo < 2 ? 3 : 1);
    let content = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${E(host.getAttribute('aria-label'))}"><title>${E(host.getAttribute('aria-label'))}，${M.date(first)} 至 ${M.date(last)}</title>`;
    for (let i = 0; i < 4; i++) {
      const value = lo + (hi - lo) * i / 3;
      content += `<line class="gridline" x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left - 8}" y="${y(value) + 4}" text-anchor="end">${axisLabel(value)}</text>`;
    }
    if (!isRatio && lo <= 0 && hi >= 0) content += `<line class="zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/>`;
    if (threshold !== null) content += `<line class="threshold" x1="${left}" x2="${right}" y1="${y(threshold)}" y2="${y(threshold)}"/><text x="${right}" y="${Math.max(12, y(threshold) - 6)}" text-anchor="end">容忍阈值 ${E(tolerance(selected[0]))}</text>`;
    selected.forEach(r => {
      const at = index(r.name) % colors.length, accessor = p => p.scenarios?.[r.name]?.[field];
      content += `<path stroke="${colors[at]}" stroke-dasharray="${dashes[at]}" d="${M.path(points, accessor, x, y)}"/>`;
      // Isolated valid samples remain visible, including the first frame after an outage.
      points.forEach((p, i) => {
        const value = accessor(p), previous = points[i - 1], next = points[i + 1];
        if (M.finite(value) && (!previous || previous.segment !== p.segment || !M.finite(accessor(previous))) && (!next || next.segment !== p.segment || !M.finite(accessor(next)))) content += `<circle cx="${x(p.ts)}" cy="${y(Number(value))}" r="3" fill="${colors[at]}"/>`;
      });
    });
    const chosen = points[sampleIndex(points, selectedTs)];
    if (chosen) content += `<line class="cursor" x1="${x(chosen.ts)}" x2="${x(chosen.ts)}" y1="${top}" y2="${bottom}"/>`;
    const ticks = width < 400 || first === last ? [0, 1] : [0, .5, 1];
    ticks.forEach((fraction, i) => {
      const ts = first + (last - first) * fraction;
      content += `<text x="${left + (right - left) * fraction}" y="${height - 9}" text-anchor="${i === 0 ? 'start' : i === ticks.length - 1 ? 'end' : 'middle'}">${M.date(ts, true)}</text>`;
    });
    host.innerHTML = content + '</svg>';
  }
  function renderCharts() {
    document.querySelectorAll('[data-range]').forEach(b => b.setAttribute('aria-pressed', b.dataset.range === state.range));
    const legend = rows().map(r => `<span class="${cls(r.name)}"><i class="key" aria-hidden="true"></i>${E(label(r))}</span>`).join('');
    $('pnl-legend').innerHTML = legend; $('ratio-legend').innerHTML = legend;
    const selected = rows()[0];
    $('threshold-note').textContent = state.strategy === 'all' ? '选中单组可查看其容忍阈值线' : selected?.tolerance_percent === null ? '不限组没有纠偏阈值' : `实际净桶数比例 · 容忍阈值 ${tolerance(selected)}`;
    drawChart('pnl-chart', 'total_pnl_usdc'); drawChart('ratio-chart', 'inventory_ratio', true);
    const points = data?.history?.points || [], at = sampleIndex(points, selectedTs), sample = points[at];
    $('sample').max = Math.max(0, points.length - 1); $('sample').value = Math.max(0, at); $('sample').disabled = !points.length;
    $('sample-time').textContent = sample ? M.date(sample.ts) : '—';
    $('sample-values').innerHTML = sample ? rows().map(r => {
      const value = sample.scenarios?.[r.name];
      return `<span class="${cls(r.name)}">${E(label(r))}：${M.signed(value?.total_pnl_usdc, 4)} USDC · 偏离 ${percent(value?.inventory_ratio)} · 方向 ${M.signed(value?.directional_barrels, 3)} 桶</span>`;
    }).join('') : '';
    const ranges = {'1h':'1 小时', '24h':'24 小时', '7d':'7 天'};
    const shownRange = data?.history?.window || data?.history?.range || state.range;
    $('history-note').textContent = points.length ? `曲线窗口 ${ranges[shownRange] || shownRange} · 原始 ${M.number(data.history.source_count, 0)} 点 / 显示 ${points.length} 点 · 缺失行情断线显示 · 采样点可用滑块、方向键或轻触查看。${shownRange !== state.range ? '所选窗口正在加载；保留上次成功窗口。' : ''}` : '曲线范围只影响历史展示，不重置累计收益或成交统计。';
  }
  function table(headers, body, extra = '') {
    return `<div class="table-wrap" tabindex="0" aria-label="${E(headers[0])}对照表"><table class="${extra}"><thead><tr>${headers.map(h => `<th scope="col">${E(h)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  function renderRisk() {
    if (!rows().length) {$('risk-table').innerHTML = $('attribution').innerHTML = '<div class="empty">等待组合账本</div>'; return;}
    const reference = data?.summary?.parameters?.risk_reference_percent;
    $('risk-note').textContent = `各组独立核算 · 统一参考比例 ${M.finite(reference) ? Number(reference) + '%' : '未提供'} · 观测时长排除行情缺口 · 累计自本轮实验开始。`;
    const headers = ['策略', '权益 / USDC', '方向 / 价差桶数', '最大偏离', '最大 / 平均方向桶数', '超参考阈值时长', '超本组阈值时长', '纠偏次数', '纠偏额 / USDC', '纠偏成本 / USDC', '保证金 / USDC'];
    $('risk-table').innerHTML = table(headers, rows().map(r => `<tr><td class="row-name ${cls(r.name)}">${E(label(r))}</td><td>${M.number(r.equity_usdc, 2)}</td><td>${M.signed(r.directional_barrels, 3)} / ${M.signed(r.spread_barrels, 3)}</td><td>${percent(r.max_inventory_ratio)}</td><td>${M.number(r.max_abs_directional_barrels, 3)} / ${M.number(r.mean_abs_directional_barrels, 3)}</td><td>${M.finite(r.exposure_seconds) ? M.number(Number(r.exposure_seconds) / 3600, 2) + ' 小时' : '—'}<span class="secondary">观测时长占比 ${percent(exposure(r), 1)}</span></td><td>${r.tolerance_percent === null ? '不适用' : M.finite(r.breach_seconds) ? M.number(Number(r.breach_seconds) / 3600, 2) + ' 小时' : '—'}</td><td>${M.number(r.hedge_adjustments, 0)}</td><td>${M.number(r.hedge_turnover_usdc, 2)}</td><td>${M.number(r.hedge_cost_usdc, 4)}</td><td>${M.number(r.margin_usdc, 2)}</td></tr>`).join(''));
    $('attribution').innerHTML = table(['策略', '方向收益', '+ 价差收益', '− 执行成本', '− 退出储备', '= 总收益', '核对'], rows().map(r => {
      const check = reconciliation(r);
      return `<tr><td class="row-name ${cls(r.name)}">${E(label(r))}</td><td>${pnl(r.direction_pnl_usdc)}</td><td>${pnl(r.spread_pnl_usdc)}</td><td>${M.number(r.execution_cost_usdc, 4)}</td><td>${M.number(r.exit_cost_reserve_usdc, 4)}</td><td>${pnl(r.total_pnl_usdc)}</td><td class="check${check.matches === false ? ' error' : ''}">${check.matches === null ? '缺少归因数据' : check.matches ? '一致' : '差额 ' + M.signed(check.difference, 8)}</td></tr>`;
    }).join(''));
  }
  const parameterLabels = {quantity_barrels:'基础下单桶数 / 桶',scalp_quantity_barrels:'Scalp 桶数 / 桶',scalp_take_profit_usdc:'Scalp 止盈 / USDC',scalp_take_profit_usdc_per_barrel:'Scalp 止盈 / USDC/桶',scalp_step_percent:'Scalp 入场间距 / %',scalp_take_profit_percent:'Scalp 止盈幅度 / %',scalp_cooldown_seconds:'Scalp 冷却 / 秒',ordinary_step_percent:'普通价格网格间距 / %',spread_step_percent:'价差网格间距 / %',spread_direction:'价差网格方向',execution_step_barrels:'配置数量步长 / 桶',effective_execution_step_barrels:'实际可成交步长 / 桶',reset_fraction:'纠偏后目标占阈值比例',risk_reference_percent:'统一风险参考比例 / %',enable_scalp:'Scalp 策略',enable_ordinary:'普通价格网格',enable_spread:'价差网格',price_grid_step_percent:'价格网格间距 / %',spread_grid_step_percent:'价差网格间距 / %',grid_step_percent:'网格间距 / %',fee_bps_per_leg:'每腿手续费 / bp',fee_bps:'每腿手续费 / bp',slippage_bps:'滑点 / bp',slippage_bps_per_leg:'每腿滑点 / bp',paper_leverage:'模拟杠杆 / 倍',initial_balance_usdc:'每组初始资金 / USDC',paper_balance_usdc:'每组初始资金 / USDC',max_drawdown_fraction:'回撤停机比例',max_holding_hours:'最长持仓 / 小时',poll_seconds:'采样周期 / 秒',center_window_hours:'中枢窗口 / 小时',scalp_reentry:'Scalp 再入场规则'};
  function renderParameters() {
    const summary = data?.summary;
    if (!summary) return '<div class="empty">等待发布实验参数</div>';
    const common = {...summary.parameters, center_window_hours:summary.center_window_hours, poll_seconds:summary.poll_seconds};
    const scalar = value => value === null ? '不限 / 未设置' : typeof value === 'object' ? JSON.stringify(value, null, 2) : typeof value === 'boolean' ? value ? '启用' : '关闭' : String(value);
    return table(['策略', '容忍阈值', '初始资金 / USDC', '杠杆 / 倍', 'Scalp a / b / a−b 桶数', '原始方向桶数', '名义持仓 / USDC', '成交量 / 桶', '成交额 / USDC', '成交笔数', '手续费 / USDC', '已实现 / USDC', '浮盈亏 / USDC'], rows().map(r => `<tr><td class="row-name ${cls(r.name)}">${E(label(r))}</td><td>${E(tolerance(r))}</td><td>${M.number(r.initial_balance_usdc, 2)}</td><td>${M.number(r.paper_leverage, 1)}</td><td>${M.number(r.scalp_cl_long_barrels, 3)} / ${M.number(r.scalp_bz_short_barrels, 3)} / ${M.signed(r.scalp_difference_barrels, 3)}</td><td>${M.signed(r.raw_directional_barrels, 3)}</td><td>${M.number(r.position_notional_usdc, 2)}</td><td>${M.number(r.volume_barrels, 3)}</td><td>${M.number(r.turnover_usdc, 2)}</td><td>${M.number(r.fill_count, 0)}</td><td>${M.number(r.fees_usdc, 4)}</td><td>${pnl(r.realized_pnl_usdc)}</td><td>${pnl(r.unrealized_pnl_usdc)}</td></tr>`).join('')) + '<dl class="parameters">' + Object.entries(common).filter(([, v]) => v !== undefined).map(([key, value]) => `<div class="parameter"><dt>${E(parameterLabels[key] || key)}</dt><dd>${E(key === 'spread_direction' ? ({long:'做多 BZ / 做空 CL',both:'双向价差网格'}[value] || scalar(value)) : scalar(value))}</dd></div>`).join('') + '</dl>';
  }
  function renderDetails() {
    document.querySelectorAll('.tabs [data-view]').forEach(b => {const active = b.dataset.view === state.view; b.setAttribute('aria-selected', active); b.tabIndex = active ? 0 : -1;});
    $('detail-content').setAttribute('aria-labelledby', 'tab-' + state.view);
    const isTrades = state.view === 'trades';
    $('export').hidden = !isTrades; $('export').disabled = !data?.details_available || !filtered(data?.trades).length;
    if (state.view === 'parameters') {
      $('detail-content').innerHTML = renderParameters(); $('pagination').hidden = true;
      $('detail-note').textContent = `当前 ${M.number(data?.summary?.center_window_hours, 0)} 小时中枢 ${M.number(data?.summary?.center, 4)} USDC/桶。手续费是执行成本的组成部分；纠偏成本也是执行成本的子集，均不可再次扣除。`;
      return;
    }
    const records = filtered(isTrades ? data?.trades : data?.positions), size = 15, pages = Math.max(1, Math.ceil(records.length / size));
    page = Math.min(page, pages - 1);
    if (!data?.details_available || !records.length) $('detail-content').innerHTML = `<div class="empty">${!data?.details_available ? '等待完整账本记录' : isTrades ? '当前筛选下暂无模拟成交' : '当前筛选下没有实际净仓位'}</div>`;
    else {
      const headers = isTrades ? ['策略', '时间', '标的 / 方向', '成交桶数', '成交价', '手续费 / USDC', '已实现 / USDC', '执行成本 / USDC', '原因'] : ['策略', '标的', '实际净桶数', '平均成本', '标记价', '浮盈亏 / USDC', '估值时间'];
      $('detail-content').innerHTML = table(headers, records.slice(page * size, (page + 1) * size).map(r => {
        const values = isTrades ? [E(rowLabel(r.scenario)), M.date(r.ts), E(r.symbol) + ' / ' + E(({buy:'买入',sell:'卖出'}[r.side] || r.side)), M.number(r.qty, 3), M.number(r.price, 4), M.number(r.fee, 4), pnl(r.realized_pnl_usdc), M.number(r.execution_cost_usdc, 4), E(reason(r.reason))] : [E(rowLabel(r.scenario)), E(r.symbol), M.signed(r.qty, 3), M.number(r.average_price, 4), M.number(r.mark_price, 4), pnl(r.unrealized_pnl_usdc), M.date(r.valued_at)];
        return '<tr>' + values.map((v, i) => `<td data-label="${E(headers[i])}"${i === 0 ? ` class="row-name ${cls(r.scenario)}"` : ''}>${v}</td>`).join('') + '</tr>';
      }).join(''), 'records-table');
    }
    $('pagination').hidden = pages <= 1;
    $('page-label').textContent = `${records.length} 条 · 第 ${page + 1} / ${pages} 页`;
    $('previous').disabled = page === 0; $('next').disabled = page === pages - 1;
    $('detail-note').textContent = isTrades ? `逐腿模拟成交；每组最多显示最近 ${data?.trade_limit_per_scenario || 100} 条。导出当前筛选已加载的 ${records.length} 条。买入 / 卖出包含开仓、减仓和纠偏，非完整交易胜负统计。` : '同组所有策略模块净额合并后的实际账户持仓。正数为多头，负数为空头；价格为 USDC/桶，时间为北京时间。';
  }
  function render() {
    const focused = document.activeElement?.dataset?.strategy;
    renderStrategies(); renderCharts(); renderRisk(); renderDetails(); status();
    if (focused) Array.from(document.querySelectorAll('[data-strategy]')).find(b => b.dataset.strategy === focused)?.focus({preventScroll:true});
  }
  async function refresh() {
    clearTimeout(timer); controller?.abort();
    if (!window.GridHub.active()) return;
    const request = controller = new AbortController(), timeout = setTimeout(() => request.abort(), 8000);
    $('refresh').disabled = true;
    try {
      const response = await fetch('/api/dashboard?range=' + encodeURIComponent(state.range), {signal:request.signal, cache:'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const next = await response.json();
      if (request !== controller || !window.GridHub.active()) return;
      if (next.summary && (next.summary.mode !== 'inventory_comparison' || !Array.isArray(next.summary.scenarios))) throw new Error('Unexpected dashboard payload');
      if (data?.reset?.generation !== next.reset?.generation) {selectedTs = null; page = 0;}
      data = next; received = Date.now();
      const serverTime = M.finite(data.server_ts) ? Number(data.server_ts) : Date.parse(data.generated_utc) / 1000;
      serverAge = data.summary ? Math.max(0, (Number.isFinite(serverTime) ? serverTime : received / 1000) - data.summary.ts) : 0;
      disconnected = false;
      if (data.summary) state = M.state(location.search, names());
      render();
    } catch (error) {
      if (request !== controller || !window.GridHub.active()) return;
      disconnected = true;
      if (data) status();
      else {$('notice').hidden = false; $('notice').textContent = '暂时无法读取监控数据，页面将自动重试。'; $('status').textContent = '连接中断'; $('status').className = 'status error';}
    } finally {
      clearTimeout(timeout);
      if (request === controller) {$('refresh').disabled = false; if (window.GridHub.active()) timer = setTimeout(refresh, 10000);}
    }
  }
  $('reset').onclick = () => {resetGeneration = data?.reset?.generation; $('reset-balances').textContent = allRows().map(r => label(r) + '：' + M.number(r.initial_balance_usdc, 2) + ' USDC').join('；'); $('reset-dialog').showModal();};
  $('reset-cancel').onclick = () => $('reset-dialog').close();
  $('reset-confirm').onclick = async () => {
    if (resetSending) return;
    resetSending = true; resetError = ''; $('reset-dialog').close(); resetStatus();
    const abort = new AbortController(), timeout = setTimeout(() => abort.abort(), 8000);
    try {
      const response = await fetch('/api/reset', {method:'POST', headers:{'Content-Type':'application/json', 'X-Reset-Token':data.reset_token}, body:JSON.stringify({generation:resetGeneration}), signal:abort.signal});
      if (!response.ok) throw new Error();
      data.reset = (await response.json()).reset;
    } catch {resetError = '重置请求结果尚未确认，请刷新查看状态；不会自动重复提交。';}
    finally {clearTimeout(timeout); resetSending = false; resetStatus(); refresh();}
  };
  $('refresh').onclick = refresh;
  $('all-strategies').onclick = () => setState({strategy:'all'});
  $('strategy-select').onchange = event => setState({strategy:event.target.value});
  $('sample').oninput = event => {selectedTs = data?.history?.points[Number(event.target.value)]?.ts ?? null; renderCharts();};
  $('previous').onclick = () => {page--; renderDetails();}; $('next').onclick = () => {page++; renderDetails();};
  document.addEventListener('click', event => {
    const strategy = event.target.closest('[data-strategy]'); if (strategy) setState({strategy:strategy.dataset.strategy});
    const range = event.target.closest('[data-range]'); if (range) {setState({range:range.dataset.range}); selectedTs = null; refresh();}
    const view = event.target.closest('[data-view]'); if (view) setState({view:view.dataset.view});
  });
  document.querySelector('.tabs').addEventListener('keydown', event => {
    const tabs = Array.from(document.querySelectorAll('.tabs [data-view]')), at = tabs.indexOf(event.target);
    if (at < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (at + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    setState({view:tabs[next].dataset.view}); tabs[next].focus();
  });
  $('export').onclick = () => {
    const values = [['策略', '成交编号', '北京时间', '标的', '方向', '桶数', '成交价USDC每桶', '手续费USDC', '已实现USDC', '执行成本USDC', '原因'], ...filtered(data.trades).map(r => [r.scenario, r.id, M.date(r.ts, false, true), r.symbol, r.side, r.qty, r.price, r.fee, r.realized_pnl_usdc, r.execution_cost_usdc, reason(r.reason)])];
    const url = URL.createObjectURL(new Blob([M.csv(values)], {type:'text/csv;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = `inventory-trades-${state.strategy}-${Date.now()}.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  window.addEventListener('popstate', () => {state = M.state(location.search, names()); page = 0; selectedTs = null; render(); refresh();});
  let resize;
  new ResizeObserver(() => {clearTimeout(resize); resize = setTimeout(renderCharts, 80);}).observe($('pnl-chart'));
  window.GridHub.subscribe(active => {if (active) refresh(); else {clearTimeout(timer); controller?.abort();}});
  setInterval(() => {if (window.GridHub.active()) status();}, 1000);
  render(); refresh();
})(typeof window !== 'undefined' ? window : globalThis);
