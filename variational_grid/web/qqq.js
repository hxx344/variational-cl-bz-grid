/* Published decimal ledger values remain authoritative; Number is used for display only. */
(function (root) {
  'use strict';
  const M = typeof module !== 'undefined' && module.exports ? require('./model.js') : root.GridModel;
  const colors = ['#285de5', '#098477', '#8051a2'], dashes = ['', '7 3', '2 3'];
  const percent = (value, digits = 2) => M.finite(value) ? M.number(value, digits) + '%' : '—';
  const dollarHedge = row => M.finite(row?.hedge_threshold_usdc);
  const hedgeLimit = row => dollarHedge(row) ? M.number(row.hedge_threshold_usdc, 0) + ' USDC' : percent(row?.hedge_tolerance_percent, 0);
  const isScalperModel = model => ['perp_dex_scalper_v1','perp_dex_scalper_v2'].includes(model);
  const isScalper = value => isScalperModel(value?.scalper?.model);
  const distanceFree = value => value?.scalper?.model === 'perp_dex_scalper_v2';
  const seconds = value => M.finite(value) ? Number(value).toLocaleString('en-US', {maximumFractionDigits:1}) + ' 秒' : '—';
  const profitTarget = row => row?.scalper?.take_profit_percent ?? row?.take_profit_percent ?? row?.grid_step_percent;
  const label = row => row ? `${isScalper(row) ? '剥头皮' : '网格'} ${distanceFree(row) ? 'TP ' + percent(profitTarget(row),2) : percent(row.grid_step_percent,2)} / 对冲 ${hedgeLimit(row)}` : '未知账户';
  const strategyTitle = row => distanceFree(row) ? `止盈 ${percent(profitTarget(row),2)}` : `${isScalper(row) ? '间距' : '网格'} ${percent(row?.grid_step_percent,2)}${isScalper(row) ? ' / TP ' + percent(profitTarget(row),2) : ''}`;
  const entryDistanceRule = value => distanceFree(value) ? '已取消；不按已有批次与当前价格的距离拦截新开仓' : '最小已有 TP ÷ [最优卖价 × (1 + TP%)] > 1 + 间距%；没有已有 TP 时通过';
  function scalperStatus(row) {
    if (!isScalper(row)) return null;
    const s = row.scalper;
    const phases = {market_gap:'QQQ 行情过期或存在缺口，暂停开仓',entry_paused:'行情受限，暂停开仓',awaiting_fill:'开仓单等待成交',cancel_pending:'等待撤单确认',cooling_down:'等待开仓冷却',grid_blocked:'价格距离不足',capacity_full:'批次已满，暂停开仓',post_only_wait:'等待可挂 Maker 的价格',opening:'已提交模拟开仓单',take_profit_pending:'等待挂出独立止盈单'};
    return {
      phase:phases[s.phase] || '等待开仓状态',
      waiting:s.cooldown_waived === true ? '本轮批次减少，已跳过冷却' : Number(s.active_entries) > 0 ? '开仓单处理中，下次冷却从入场完成起算' : M.finite(s.cooldown_remaining_seconds) ? `采样时冷却剩余 ${seconds(Math.max(0, Number(s.cooldown_remaining_seconds)))}` : '采样时冷却剩余未提供',
      gate:distanceFree(row) ? '新开仓距离门槛已取消' : s.grid_allowed === true ? '价格距离已满足' : s.grid_allowed === false ? '价格距离未满足' : '价格距离未评估',
      orders:`开仓 ${M.number(s.active_entries,0)} / 1 · TP ${M.number(s.active_take_profits,0)} · 占用 ${M.number(s.occupied_batches,0)} / ${M.number(s.max_batches,0)} 批`,
      prices:`候选开仓 ${M.number(s.candidate_entry_price,4)} / TP ${M.number(s.candidate_tp_price,4)} USDC`,
      pending:['market_gap','entry_paused','cancel_pending','grid_blocked','capacity_full','post_only_wait','take_profit_pending'].includes(s.phase)
    };
  }
  function fillReason(fill) {
    const value = fill?.reason;
    if (isScalperModel(fill?.maker_model) && ['maker_entry','maker_take_profit'].includes(value)) return value === 'maker_entry' ? 'Maker 剥头皮开仓' : 'Maker 批次止盈';
    return ({grid_entry:'网格买入',grid_buy:'网格买入',maker_entry:'Maker 网格买入',maker_take_profit:'Maker 网格止盈',grid_take_profit:'网格止盈',grid_tp:'网格止盈',take_profit:'止盈',delta_hedge:'敞口对冲调整',hedge:'空头对冲',hedge_open:'增加空头对冲',hedge_reduce:'减少空头对冲',rebalance:'对冲调整',hedge_rebalance:'对冲调整',initial:'初始建仓'}[value] || value || '—');
  }
  function entryProgress(row, data, elapsedSeconds = 0, disconnected = false) {
    if (!isScalper(row)) return null;
    const s = row.scalper, summary = data?.summary, market = summary?.market;
    const valid = value => typeof value !== 'boolean' && M.finite(value);
    const elapsed = valid(elapsedSeconds) ? Math.max(0, Number(elapsedSeconds)) : 0;
    const clockKnown = valid(data?.server_ts) && valid(summary?.ts) && Number(data.server_ts) >= Number(summary.ts);
    const age = clockKnown ? Number(data.server_ts) - Number(summary.ts) + elapsed : null;
    const stale = age !== null && age > Math.max(60, valid(summary?.poll_seconds) ? Number(summary.poll_seconds) * 3 : 60);
    const reference = referenceStatus(data, elapsed);
    const frozen = disconnected ? '连接中断' : data?.runtime?.status !== 'running' ? '服务未正常运行' : !clockKnown ? '采样时间未提供' : stale ? '采样已过期' : market?.gap || !['ready','ok','running','live','live_indicative','synthetic'].includes(market?.source_status) || ['market_gap','entry_paused'].includes(s.phase) ? '行情受限' : reference && !reference.usable ? '参考报价不可用' : '';
    const basis = frozen ? `采样值 · ${frozen}，暂停推算` : '倒计时估算 · 开仓以策略采样为准';
    const result = {percent:null, remaining:null, total:null, basis:frozen ? basis : '开仓条件以末次采样为准', frozen:!!frozen};
    // An active order still carries the previous entry timestamp; it is not the next cooldown.
    if (Number(s.active_entries) > 0 || ['opening','awaiting_fill','cancel_pending'].includes(s.phase)) {
      return {...result, mode:'order', value:'处理中', detail:'开仓单成交或撤单确认后，再计算下一轮冷却'};
    }
    if (s.cooldown_waived === true) return {...result, mode:'waived', percent:100, value:'免等待', detail:'本轮已跳过冷却，仍需满足其他开仓条件'};
    if (s.next_entry_at === null && valid(s.cooldown_remaining_seconds) && Number(s.cooldown_remaining_seconds) === 0) {
      return {...result, mode:'first', percent:100, value:'首次开仓', detail:'首次无需冷却，等待策略检查开仓条件'};
    }
    if (!valid(s.cooldown_seconds) || !valid(s.cooldown_remaining_seconds) || Number(s.cooldown_seconds) < 0 || (Number(s.cooldown_seconds) === 0 && Number(s.cooldown_remaining_seconds) > 0)) {
      return {...result, mode:'unknown', value:'—', detail:'等待有效冷却数据'};
    }
    const total = Number(s.cooldown_seconds);
    const remaining = Math.min(total, Math.max(0, Number(s.cooldown_remaining_seconds) - (frozen ? 0 : age)));
    const progress = total === 0 ? 100 : (1 - remaining / total) * 100;
    // Never round an unfinished cooldown up to 100% or its remaining time down to zero.
    const shownPercent = remaining === 0 ? 100 : Math.floor(progress * 10) / 10;
    return {...result, mode:'timed', percent:shownPercent, remaining, total, basis, value:percent(shownPercent,1),
      detail:remaining > 0 ? `剩余 ${seconds(Math.ceil(remaining * 10) / 10)} / 本轮 ${seconds(total)}` : '冷却已到，等待下一次采样检查开仓条件'};
  }
  function encoding(row) {
    return {color:Math.max(0, [.05, .1, .2].indexOf(Number(row?.grid_step_percent))), dash:Math.max(0, [0, 2, 5].indexOf(Number(row?.hedge_tolerance_percent)))};
  }
  function reconciliation(row) {
    const values = [row?.qqq?.total_pnl_usdc, row?.us100?.total_pnl_usdc, row?.total_pnl_usdc];
    if (!values.every(M.finite)) return null;
    return Math.abs(Number(values[0]) + Number(values[1]) - Number(values[2])) <= Math.max(1e-8, ...values.map(v => Math.abs(Number(v)) * 1e-10));
  }
  function sampleIndex(points, ts) {
    if (!points.length) return -1;
    if (ts === null) return points.length - 1;
    let nearest = 0;
    points.forEach((p, i) => {if (Math.abs(p.ts - ts) < Math.abs(points[nearest].ts - ts)) nearest = i;});
    return nearest;
  }
  function exposureDomain(values, threshold) {
    const valid = values.filter(M.finite).map(v => Math.abs(Number(v)));
    if (M.finite(threshold)) valid.push(Math.abs(Number(threshold)));
    const bound = Math.min(100, Math.max(5, ...valid) * 1.15);
    return [-bound, bound];
  }
  function historyValue(history, point, name, field) {
    const at = (history?.names || []).indexOf(name);
    return at >= 0 && M.finite(point?.[field]?.[at]) ? Number(point[field][at]) : null;
  }
  function freshness(data, serverAge, received, now, disconnected) {
    const age = data?.summary ? Math.max(0, serverAge + (now - received) / 1000) : null;
    const stale = age !== null && age > Math.max(60, Number(data.summary.poll_seconds || 0) * 3);
    const runtime = data?.runtime?.status;
    const names = {running:'模拟运行中', degraded:'部分行情可用', paused:'行情暂停', stopped:'模拟已停止', starting:'等待行情', resetting:'正在重置'};
    return {age, stale, label:disconnected ? '页面连接中断' : stale ? '行情已过期' : names[runtime] || '等待有效行情', warning:disconnected || stale || runtime !== 'running'};
  }
  function marketStatus(data, freshnessState, disconnected) {
    if (!data?.summary) return '等待行情';
    const source = data.summary.market?.source_status;
    const previous = ({ok:'末次行情可用',ready:'末次行情可用',running:'末次行情可用',live:'末次公共行情可用',live_indicative:'末次公共行情 / 指示性报价',synthetic:'末次合成行情演示',paused_entries:'末次行情受限 · 暂停新开仓',paused:'末次行情暂停',stale:'末次行情过期',partial:'末次行情部分缺失',offline:'末次行情离线',gap:'末次行情存在缺口',waiting:'等待行情'}[source] || '末次行情状态未知');
    const prefix = disconnected ? '连接中断' : freshnessState.stale ? '已过期' : ({stopped:'已停止',paused:'已暂停',resetting:'正在重置'}[data.runtime?.status]);
    return prefix ? `${prefix} · ${previous}` : previous;
  }
  function hedgeStatus(row) {
    const statuses = {
      quantity_rounding_residual:'数量精度尾差 · 保留实际敞口',
      var_close_only:'待对冲 · US100 仅允许减仓',
      hedge_quote_unavailable_or_below_minimum:'待对冲 · 报价不可用或未达最小对冲量',
      hedge_below_minimum:'未达最小对冲量 · 保留实际敞口',
      hedge_quote_unavailable:'待对冲 · 对冲报价不可用',
      inside_band:row?.hedge_pending ? '数量精度尾差 · 保留实际敞口' : '敞口在容忍范围内',
      hedged:'已完成对冲',
      var_market_unavailable:'待对冲 · US100 行情不可用'
    };
    return statuses[row?.hedge_status] || (row?.hedge_pending ? '待对冲 · 原因未提供' : '无待处理对冲');
  }
  function cooldownNotice(data, elapsedSeconds) {
    const now = Number(data?.server_ts) + Math.max(0, elapsedSeconds);
    return (data?.rate_limits || []).filter(r => ['Lighter', 'Variational'].includes(r.venue) && M.finite(r.retry_at))
      .map(r => `${r.venue} HTTP 429 限流：${Number(r.retry_at) > now ? Math.ceil(Number(r.retry_at) - now) + ' 秒后重试' : '冷却结束，等待下一次行情结果'}`).join('；');
  }
  function dollarExposureDomain(values, threshold) {
    const valid = values.filter(M.finite).map(v => Math.abs(Number(v)));
    if (M.finite(threshold)) valid.push(Math.abs(Number(threshold)));
    const bound = Math.max(1, ...valid) * 1.15;
    return [-bound, bound];
  }
  function referenceStatus(data, elapsedSeconds = 0) {
    const cache = data?.summary?.market?.quote_cache;
    if (!cache || cache.mode !== 'shared_indicative_v1') return null;
    const age = M.finite(cache.source_ts) && M.finite(data.server_ts) ? Math.max(0, Number(data.server_ts) + Math.max(0, elapsedSeconds) - Number(cache.source_ts)) : null;
    const usable = cache.available === true && age !== null && age <= Number(cache.max_age_seconds);
    const label = age === null ? '等待首份参考报价' : !usable ? `参考价不可用 · 报价已有 ${Math.floor(age)} 秒` : `${cache.cache_used ? '缓存估算' : '共享参考价估算'} · 报价已有 ${Math.floor(age)} 秒`;
    const auth = cache.authentication === 'vr-token' ? (cache.authenticated ? 'Var token 已验证' : 'Var token 未就绪') : '';
    return {age, usable, label:auth ? `${auth} · ${label}` : label, limit:cache.max_age_seconds, error:cache.refresh_error || ''};
  }
  function fillPricing(row) {
    if (row?.pricing_mode === 'shared_indicative_v1') return `${row.cache_used ? '缓存参考价估算' : '共享参考价估算'} · 报价龄 ${M.number(row.quote_age_seconds, 1)} 秒 · 源数量 ${M.number(row.source_qty, 6)}${row.half_spread_percent == null ? '' : ' · 半点差 ' + percent(row.half_spread_percent, 4)}`;
    return row?.venue === 'Variational' ? '原精确数量报价' : isScalperModel(row?.maker_model) ? '剥头皮 · 严格 Maker 队列模拟' : 'Maker 队列模拟';
  }
  const helpers = {percent, label, isScalper, distanceFree, strategyTitle, entryDistanceRule, seconds, scalperStatus, entryProgress, fillReason, encoding, reconciliation, sampleIndex, exposureDomain, dollarExposureDomain, dollarHedge, hedgeLimit, historyValue, freshness, marketStatus, hedgeStatus, cooldownNotice, referenceStatus, fillPricing};
  if (typeof module !== 'undefined' && module.exports) {module.exports = helpers; return;}
  root.QQQModel = helpers;
  const $ = id => document.getElementById(id), E = M.escape;
  let data = null, state = M.state(location.search, []), page = 0, selectedTs = null;
  let timer = null, controller = null, disconnected = false, received = 0, serverAge = 0;
  let progressReceived = 0;
  let resetSending = false, resetError = '', resetGeneration = null;
  const allRows = () => data?.summary?.scenarios || [];
  const names = () => allRows().map(r => r.name);
  const rows = () => allRows().filter(r => state.strategy === 'all' || r.name === state.strategy);
  const filtered = values => (values || []).filter(r => state.strategy === 'all' || r.scenario === state.strategy);
  const rowLabel = name => label(allRows().find(r => r.name === name));
  const cls = row => {const e = encoding(row); return `s${e.color} d${e.dash}`;};
  const pnl = value => `<span class="${M.tone(value)}">${M.signed(value, 2)}</span>`;
  const sourceReason = value => ({'Synthetic demonstration; not a historical backtest':'合成行情演示，用于查看模拟行为，不是历史回测。','US100 market closed; maker entries paused':'US100 市场关闭，暂停新开仓。','US100 quote stale; maker entries paused':'US100 报价过期，暂停新开仓。','US100 quote expired while gathering hedge prices':'收集对冲报价期间 US100 报价过期，等待有效行情。'}[value] || value || '等待数据恢复');
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
    if (busy) message = r.status === 'pending' ? '重置请求已保存，等待模拟进程完成当前采样；进程停止时需先启动模拟服务。' : '正在归档并重置全部模拟账户…';
    else if (r?.status === 'complete') message = '旧账本已归档，当前为新一轮模拟。归档编号 ' + r.archive_id;
    else if (r?.status === 'failed') message = '归档失败，原模拟数据已保留；请检查磁盘空间与服务日志后重试。';
    $('reset-message').textContent = message; $('reset-message').hidden = !message;
  }
  function status() {
    resetStatus();
    if (!data) return;
    const s = data.summary, f = freshness(data, serverAge, received, Date.now(), disconnected), runtime = data.runtime?.status;
    $('status').textContent = f.label;
    $('status').className = 'status' + (disconnected ? ' error' : f.warning ? ' warn' : '');
    $('market-status').textContent = marketStatus(data, f, disconnected);
    $('freshness').textContent = f.age === null ? '等待第一份有效采样' : `${M.date(s.ts)} · ${Math.floor(f.age)} 秒前 · 每 ${M.number(s.poll_seconds, 0)} 秒采样`;
    let notice = '';
    const elapsed = (Date.now() - received) / 1000, cooldown = cooldownNotice(data, elapsed), reference = referenceStatus(data, elapsed);
    if (reference) $('us100-source-time').textContent = `${M.date(s?.market?.var_source_ts ?? s?.market?.quote_cache?.source_ts)} · ${reference.label}`;
    if (disconnected) notice = '无法连接监控服务，保留上次成功读取的数据。页面会自动重试。';
    else if (runtime === 'stopped') notice = '模拟进程已停止，以下为最后保存的数据。';
    else if (runtime === 'paused') notice = '行情暂停，保留最近有效采样：' + sourceReason(data.runtime.reason);
    else if (cooldown) notice = cooldown + (reference?.usable && s?.market?.source_status === 'ready' ? `。${reference.label}，模拟继续。` : '。暂停新开仓，保留已有仓位和已确认的模拟成交。');
    else if (f.stale) notice = '行情已过期，收益和仓位估值停留在最后有效采样。';
    else if (reference && !reference.usable) notice = `${reference.label}。${reference.error || '等待刷新'}；缓存最长使用 ${reference.limit} 秒。`;
    else if (reference?.error && s?.market?.source_status === 'ready') notice = `刷新暂缓，${reference.label}，模拟继续。${reference.error}`;
    else if (s?.market?.gap) notice = '公共成交序列存在缺口；保留已知仓位与损益，暂停新开仓。';
    else if (s?.market?.source_reason) notice = sourceReason(s.market.source_reason);
    else if (s && !data.details_available) notice = '汇总可用，对应仓位或成交明细暂不可用。';
    $('notice').textContent = notice; $('notice').hidden = !notice;
    updateEntryProgress();
  }
  function updateEntryProgress() {
    const elapsed = Math.max(0, (performance.now() - progressReceived) / 1000);
    for (const node of $('strategies').querySelectorAll('[data-entry-progress]')) {
      const row = allRows()[Number(node.dataset.entryProgress)], p = entryProgress(row, data, elapsed, disconnected);
      if (!p) continue;
      node.classList.toggle('frozen', p.frozen);
      node.classList.toggle('inactive', p.percent === null);
      node.querySelector('.entry-progress-fill').setAttribute('width', p.percent ?? 0);
      for (const key of ['value','detail','basis']) {
        const target = node.querySelector('[data-progress-' + key + ']');
        if (target.textContent !== p[key]) target.textContent = p[key];
      }
    }
  }
  function renderOverview() {
    const s = data?.summary, market = s?.market, scalper = isScalper(s);
    const title = scalper ? 'QQQ / US100 对冲剥头皮' : s ? 'QQQ / US100 对冲网格' : 'QQQ / US100 模拟监控';
    document.title = title; $('page-title').textContent = title;
    $('strategy-description').textContent = scalper ? 'Lighter QQQ 单张近盘口开仓、逐批止盈，Variational US100 做空对冲。' : s ? 'Lighter QQQ 只做多网格，Variational US100 做空对冲。' : 'Lighter QQQ 做多，Variational US100 做空对冲。';
    $('tab-positions').textContent = scalper ? 'QQQ 批次持仓' : s ? 'QQQ 网格持仓' : 'QQQ 持仓';
    for (const leg of ['qqq','us100']) {
      $(leg + '-mark').textContent = M.number(market?.[leg + '_mark'], 2);
      $(leg + '-spread').textContent = `买 ${M.number(market?.[leg + '_bid'], 2)} / 卖 ${M.number(market?.[leg + '_ask'], 2)} USDC`;
    }
    $('qqq-source-time').textContent = `${market?.qqq_source_time_kind === 'observed' ? '接收时间' : '源行情时间'} ${M.date(market?.qqq_source_ts)}`;
    $('us100-source-time').textContent = `报价时间 ${M.date(market?.var_source_ts)}`;
    $('data-kind').textContent = s?.data_kind === 'synthetic' ? '合成行情演示 · 非实时市场数据' : s?.data_kind === 'live_indicative' ? '公共行情 · 指示性报价 · 模拟成交' : '模拟交易 · 等待行情';
    $('strategy-select').innerHTML = '<option value="all">全部账户</option>' + allRows().map(r => `<option value="${E(r.name)}">${E(label(r))}</option>`).join('');
    $('strategy-select').value = state.strategy;
    if (!allRows().length) {
      $('strategies').innerHTML = '<div class="empty">暂无有效采样。收到共同市场行情后，将显示本轮独立模拟账户。</div>';
      $('account-total').textContent = '';
      return;
    }
    $('experiment-info').textContent = `${allRows().length} 个独立账户 · ${scalper ? '最多 ' : ''}${M.number(s.parameters?.grid_count ?? 30, 0)} ${scalper ? '批 × 每批' : '格 × 每格'} ${M.number(s.parameters?.order_notional_usdc ?? 1000, 0)} USDC · ${M.number(s.sample_count, 0)} 次共同采样${scalper ? ' · 开仓条件为末次采样状态' : ''}`;
    const focusedStrategy = document.activeElement?.dataset?.strategy;
    $('strategies').innerHTML = allRows().map((r, i) => {
      const entry = scalperStatus(r);
      const entryPanel = entry ? `<div class="entry-state"><div class="entry-progress" data-entry-progress="${i}"><div class="entry-progress-head"><span>下一次开仓 · 冷却进度</span><b data-progress-value>—</b></div><svg class="entry-progress-track" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false"><rect class="entry-progress-background" width="100" height="8" rx="4"/><rect class="entry-progress-fill" width="0" height="8" rx="4"/></svg><span class="entry-progress-detail" data-progress-detail>等待有效冷却数据</span><span class="entry-progress-basis" data-progress-basis></span></div><strong class="${entry.pending ? 'pending' : ''}">采样时：${E(entry.phase)}</strong><span>${E(entry.gate)} · ${E(entry.orders)}</span><span>${E(entry.prices)}</span></div>` : '';
      return `<button class="strategy ${cls(r)} ${state.strategy === r.name ? 'selected' : ''}" data-strategy="${E(r.name)}" aria-pressed="${state.strategy === r.name}"><div class="strategy-head"><h3>${E(strategyTitle(r))}</h3><small>对冲阈值 ${hedgeLimit(r)}</small></div>${entryPanel}<span class="pnl-label">两腿累计净收益</span><strong class="big-pnl ${M.tone(r.total_pnl_usdc)}">${M.signed(r.total_pnl_usdc, 2)}<small>USDC</small></strong><div class="leg-pnls"><span>QQQ <b>${pnl(r.qqq?.total_pnl_usdc)}</b></span><span>US100 <b>${pnl(r.us100?.total_pnl_usdc)}</b></span></div><div class="strategy-stats"><div><span>${dollarHedge(r) ? '绝对净敞口 / 对冲阈值' : '实际敞口 / 容忍阈值'}</span><b>${dollarHedge(r) ? M.number(Math.abs(Number(r.net_exposure_usdc)),2) : percent(r.exposure_percent)} / ${hedgeLimit(r)}</b></div><div><span>净 / 总敞口（USDC）</span><b>${M.signed(r.net_exposure_usdc, 2)} / ${M.number(r.gross_exposure_usdc, 2)}</b></div><div><span>${entry ? '本档冷却' : '持仓格 / 待成交单'}</span><b>${entry ? E(seconds(r.scalper.cooldown_seconds)) : M.number(r.open_slots, 0) + ' / ' + M.number(r.resting_orders, 0)}</b></div><div><span>最大回撤 / USDC</span><b>${M.number(r.max_drawdown_usdc, 2)}</b></div></div><p class="control-status ${r.hedge_pending ? 'pending' : ''}">${E(hedgeStatus(r))}${reconciliation(r) === false ? ' · 两腿损益核对异常' : ''}</p></button>`;
    }).join('');
    if (focusedStrategy) Array.from($('strategies').children).find(node => node.dataset.strategy === focusedStrategy)?.focus({preventScroll:true});
    const values = allRows().map(r => r.total_pnl_usdc), total = values.every(M.finite) ? values.reduce((sum, value) => sum + Number(value), 0) : null;
    $('account-total').textContent = `${allRows().length} 账户净收益合计 ${M.signed(total, 2)} USDC · 仅为独立实验账户相加，不代表一个账户的收益率。`;
  }
  function drawChart(id, field) {
    const host = $(id), points = data?.history?.points || [], isExposure = field !== 'pnl', isDollar = field === 'net_exposure';
    if (!points.length) {host.innerHTML = '<div class="empty">等待有效历史采样</div>'; return;}
    const selected = rows(), width = Math.max(250, host.clientWidth), height = host.clientHeight || 280;
    const left = isExposure ? 52 : 62, right = width - 15, top = 22, bottom = height - 31;
    const threshold = isExposure && isDollar ? Number(selected[0]?.hedge_threshold_usdc) : isExposure && state.strategy !== 'all' && M.finite(selected[0]?.hedge_tolerance_percent) ? Number(selected[0].hedge_tolerance_percent) : null;
    const get = (point, name) => historyValue(data.history, point, name, field);
    const values = points.flatMap(p => selected.map(r => get(p, r.name)));
    const [lo, hi] = isDollar ? dollarExposureDomain(values, threshold) : isExposure ? exposureDomain(values, threshold) : M.domain(values, true);
    const first = Number(points[0].ts), last = Number(points.at(-1).ts);
    const x = ts => first === last ? (left + right) / 2 : left + (ts - first) / (last - first) * (right - left);
    const y = value => bottom - (value - lo) / (hi - lo) * (bottom - top);
    let content = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${E(host.getAttribute('aria-label'))}"><title>${E(host.getAttribute('aria-label'))}，${M.date(first)} 至 ${M.date(last)}；完整采样值见下方滑块。</title>`;
    for (let i = 0; i < 5; i++) {
      const value = lo + (hi - lo) * i / 4;
      content += `<line class="gridline" x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left - 8}" y="${y(value) + 4}" text-anchor="end">${isExposure && !isDollar ? percent(value, hi <= 10 ? 1 : 0) : M.number(value, hi - lo < 2 ? 2 : 0)}</text>`;
    }
    if (lo <= 0 && hi >= 0) content += `<line class="zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/>`;
    if (threshold !== null) {
      for (const value of threshold === 0 ? [0] : [-threshold, threshold]) content += `<line class="threshold" x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}"/>`;
      content += `<text x="${right}" y="${Math.max(12, y(threshold) - 6)}" text-anchor="end">阈值 ${threshold ? '±' : ''}${isDollar ? M.number(threshold,0) + ' USDC' : percent(threshold, 0)}</text>`;
    }
    selected.forEach(r => {
      const e = encoding(r), accessor = p => get(p, r.name);
      content += `<path stroke="${colors[e.color]}" stroke-dasharray="${dashes[e.dash]}" d="${M.path(points, accessor, x, y)}"/>`;
      points.forEach((point, i) => {
        const value = accessor(point), previous = points[i - 1], next = points[i + 1];
        if (M.finite(value) && (!previous || previous.segment !== point.segment || !M.finite(accessor(previous))) && (!next || next.segment !== point.segment || !M.finite(accessor(next)))) content += `<circle cx="${x(point.ts)}" cy="${y(value)}" r="3" fill="${colors[e.color]}"/>`;
      });
    });
    const chosen = points[sampleIndex(points, selectedTs)];
    if (chosen) content += `<line class="cursor" x1="${x(chosen.ts)}" x2="${x(chosen.ts)}" y1="${top}" y2="${bottom}"/>`;
    const ticks = first === last ? [.5] : width < 420 ? [0, 1] : [0, .5, 1];
    ticks.forEach(fraction => {
      const ts = first + (last - first) * fraction;
      content += `<text x="${left + (right - left) * fraction}" y="${height - 9}" text-anchor="${fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'}">${M.date(ts, true)}</text>`;
    });
    host.innerHTML = content + '</svg>';
  }
  function renderCharts() {
    document.querySelectorAll('[data-range]').forEach(b => b.setAttribute('aria-pressed', b.dataset.range === state.range));
    const legend = rows().map(r => `<span class="${cls(r)}"><i class="key" aria-hidden="true"></i>${E(label(r))}</span>`).join('');
    $('pnl-legend').innerHTML = legend; $('exposure-legend').innerHTML = legend;
    const dollars = dollarHedge(allRows()[0]), exposureField = dollars ? 'net_exposure' : 'exposure';
    $('exposure-title').textContent = dollars ? '有向净敞口 / USDC' : '有向美元敞口率 / %';
    $('exposure-chart').setAttribute('aria-label', dollars ? '实际两腿有向净敞口金额曲线，单位USDC' : '实际两腿有向美元敞口率曲线，单位百分比');
    $('threshold-note').textContent = dollars ? `净多为正，净空为负 · 超过 ±${hedgeLimit(allRows()[0])} 触发 · 调回半阈值` : state.strategy === 'all' ? '净敞口 ÷ 总敞口 · 选中账户查看正负阈值' : `实际两腿名义金额 · 正值净多 / 负值净空 · 容忍 ${hedgeLimit(rows()[0])}`;
    drawChart('pnl-chart', 'pnl'); drawChart('exposure-chart', exposureField);
    const points = data?.history?.points || [], at = sampleIndex(points, selectedTs), sample = points[at];
    $('sample').max = Math.max(0, points.length - 1); $('sample').value = Math.max(0, at); $('sample').disabled = !points.length;
    $('latest-sample').disabled = !points.length || selectedTs === null;
    $('sample-time').textContent = sample ? M.date(sample.ts) : '—';
    $('sample-values').innerHTML = sample ? rows().map(r => `<span class="${cls(r)}">${E(label(r))}：${M.signed(historyValue(data.history, sample, r.name, 'pnl'), 2)} USDC · 敞口 ${dollars ? M.signed(historyValue(data.history, sample, r.name, exposureField),2) + ' USDC' : percent(historyValue(data.history, sample, r.name, exposureField))}</span>`).join('') : '';
    const ranges = {'1h':'1 小时','24h':'24 小时','7d':'7 天'}, shownRange = data?.history?.range || state.range;
    $('history-note').textContent = points.length ? `窗口 ${ranges[shownRange] || shownRange} · 原始 ${M.number(data.history.source_count, 0)} 点 / 显示 ${points.length} 点 · 行情缺口断线显示 · 用滑块、方向键或轻触查看采样。${shownRange !== state.range ? '所选窗口正在加载，保留上次成功窗口。' : ''}` : '曲线范围只影响历史展示，不重置累计收益或成交统计。';
  }
  function table(headers, body, records = true) {
    return `<div class="table-wrap" tabindex="0" aria-label="${E(headers.join('、'))}"><table${records ? ' class="records-table"' : ''}><thead><tr>${headers.map(h => `<th scope="col">${E(h)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  function cells(headers, values, row) {
    return values.map((value, i) => `<td data-label="${E(headers[i])}"${i === 0 && row ? ` class="row-name ${cls(row)}"` : ''}>${value}</td>`).join('');
  }
  function renderLedgers() {
    if (!rows().length) {$('leg-ledger').innerHTML = $('volume-ledger').innerHTML = '<div class="empty">等待账户账本</div>'; return;}
    const headers = ['账户 / 标的','仓位数量','名义金额 / USDC','持仓均价','估值价格','已实现 / USDC','未实现 / USDC','手续费 / USDC','净损益 / USDC'];
    $('leg-ledger').innerHTML = table(headers, rows().flatMap(r => ['qqq','us100'].map(key => {
      const leg = r[key], venue = key === 'qqq' ? 'Lighter QQQ' : 'Variational US100';
      return `<tr>${cells(headers, [`<span>${E(label(r))}<span class="secondary">${venue}</span></span>`,M.signed(leg?.qty, 6),M.number(M.finite(leg?.notional_usdc) ? Math.abs(Number(leg.notional_usdc)) : null, 2),M.number(leg?.average_entry, 4),M.number(leg?.mark, 4),pnl(leg?.realized_pnl_usdc),pnl(leg?.unrealized_pnl_usdc),M.number(leg?.fees_usdc, 4),pnl(leg?.total_pnl_usdc)],r)}</tr>`;
    })).join(''));
    const volumeHeaders = ['账户','QQQ 成交量 / QQQ','US100 成交量 / US100','QQQ 成交额 / USDC','US100 成交额 / USDC','合计成交额 / USDC','组合已实现 / USDC','组合未实现 / USDC','两腿净损益核对'];
    $('volume-ledger').innerHTML = table(volumeHeaders, rows().map(r => {
      const check = reconciliation(r);
      return `<tr>${cells(volumeHeaders, [E(label(r)),M.number(r.qqq?.volume_units, 6),M.number(r.us100?.volume_units, 6),M.number(r.qqq?.turnover_usdc, 2),M.number(r.us100?.turnover_usdc, 2),M.number(r.turnover_usdc, 2),pnl(r.realized_pnl_usdc),pnl(r.unrealized_pnl_usdc),`<span class="check ${check === false ? 'error' : ''}">${check === null ? '缺少数据' : check ? '一致' : '不一致'}</span>`],r)}</tr>`;
    }).join(''));
  }
  function renderDetails() {
    document.querySelectorAll('[data-view]').forEach(button => {button.setAttribute('aria-selected', button.dataset.view === state.view); button.tabIndex = button.dataset.view === state.view ? 0 : -1;});
    $('detail-content').setAttribute('aria-labelledby', 'tab-' + state.view);
    $('export').hidden = state.view !== 'trades'; $('export').disabled = !filtered(data?.trades).length;
    $('pagination').hidden = true;
    if (state.view === 'parameters') {
      const p = data?.summary?.parameters || {}, scalper = isScalper(data?.summary), config = data?.summary?.scalper;
      const values = [['交易模式','仅模拟，不提交真实订单'],['账户组合',`${allRows().length} 个独立账户`],['QQQ 策略',scalper ? `LONG ONLY · 单张近盘口开仓 · 最多 ${M.number(p.grid_count,0)} 批` : `LONG ONLY · 固定锚点网格 · ${M.number(p.grid_count, 0)} 格`],[scalper ? '每批名义金额' : '每格名义金额',`${M.number(p.order_notional_usdc, 0)} USDC`],[scalper ? '新开仓距离门槛' : '网格间距',distanceFree(data?.summary) ? '已取消' : [...new Set(allRows().map(r => percent(r.grid_step_percent)))].join(' / ')],['对冲阈值',[...new Set(allRows().map(hedgeLimit))].join(' / ')],['对冲标的','Variational US100 空头'],['对冲目标',`美元名义金额 β = ${M.number(p.beta, 2)}`],['QQQ / US100 手续费',`${M.number(p.lighter_fee_bps, 2)} / ${M.number(p.var_fee_bps, 2)} bps`],['US100 额外滑点',`${M.number(p.var_slippage_bps, 2)} bps`],['对冲次数',rows().map(r => label(r) + '：' + M.number(r.hedge_adjustments, 0)).join(' / ')],['阈值口径',dollarHedge(allRows()[0]) ? '两腿净名义金额绝对值 / USDC' : '|净敞口| ÷ 两腿总敞口'],['QQQ 成交模型','公共成交 + 保守 Maker 队列'],['US100 成交模型','指示性买卖报价'],['资金费、隔夜费与股息调整','未计入损益']];
      if (scalper) {
        const wait = M.finite(config.wait_seconds) ? Number(config.wait_seconds) : null;
        values.splice(5,0,
          ['逐批止盈目标',[...new Set(allRows().map(r => percent(r.scalper?.take_profit_percent)))].join(' / ')],
          ['开仓距离规则',entryDistanceRule(data.summary)],
          ['基础等待',seconds(wait)],
          ['按待止盈批次数等待',`0–4 批 ${seconds(wait === null ? null : wait / 4)}；5–9 批 ${seconds(wait === null ? null : wait / 2)}；10–19 批 ${seconds(wait)}；20–29 批 ${seconds(wait === null ? null : wait * 2)}`],
          ['开仓资格',distanceFree(data.summary) ? '首次无需冷却；待止盈批次数比上次决策减少时，本轮免冷却；仍须满足行情、容量、止盈覆盖和 Maker 挂单条件' : '首次可立即开仓；待止盈批次数比上次决策减少时，本轮免冷却；价格条件失败也消耗本轮豁免，仍须满足容量'],
          ['开仓改价',`${seconds(config.reprice_after_seconds)} 后允许改价，每 ${seconds(config.reprice_poll_seconds)} 检查；按实际采样处理`],
          ['占用容量','已有持仓与待成交开仓占用批次；达到上限时暂停新开仓'],
          ['逐批止盈','开仓完全成交，或部分成交后撤单确认，再为已成交数量挂独立 TP'],
          ['开仓取价','盘口中价与最小已有 TP 减一 tick 取较小值；按 tick 四舍五入，并限制在最优卖价减一 tick 以内'],
          ['止盈取价','本批入场价格 × (1 + TP%)，按 tick 向下取整'],
          ['与原版 Lighter 的差异',`${distanceFree(data.summary) ? 'v2 已取消原版开仓距离门槛；' : ''}本地严格 Maker 队列模拟，不复刻原版 GTT 可能吃单的行为`],
          ['采样状态',`卡片冷却进度为时间推算，异常时恢复采样值；冷却结束仍需${distanceFree(data.summary) ? '行情、容量和 Maker 挂单条件' : '行情、价格距离和容量'}允许`]
        );
      }
      if (data?.summary?.pricing) {
        const pricing = data.summary.pricing;
        values.push(['半点差',pricing.half_spread_percent == null ? '使用源买卖价' : percent(pricing.half_spread_percent,4)],['US100 报价模式','各组共享参考价估算 · 未按每笔数量单独询价'],['直接复用 / 失败兜底',`${pricing.refresh_after_seconds} 秒内复用，超时先刷新；失败最多用 ${pricing.max_age_seconds} 秒旧价`],['当前报价口径起始',M.date(Date.parse(data.summary.pricing_since_utc) / 1000)],['参考估价 / 其中缓存成交',rows().map(r => `${label(r)}：${M.number(r.pricing_stats?.reference_price_fills,0)} / ${M.number(r.pricing_stats?.cached_price_fills,0)}`).join('；')]);
      }
      $('detail-content').innerHTML = `<dl class="parameters">${values.map(([k,v]) => `<div class="parameter"><dt>${E(k)}</dt><dd>${E(v)}</dd></div>`).join('')}</dl>`;
      $('detail-note').textContent = '两腿总敞口为各腿实际名义金额绝对值之和。持仓、挂单和待对冲状态来自同一份已发布采样。';
      return;
    }
    const trades = state.view === 'trades', scalper = isScalper(data?.summary), values = filtered(trades ? data?.trades : data?.positions), size = 18;
    page = Math.max(0, Math.min(page, Math.ceil(values.length / size) - 1));
    $('detail-note').textContent = trades ? '显示服务端发布的近期成交；导出遵循当前账户筛选，时间为北京时间。每条记录都是模拟成交，模型来源以该条成交为准。' : scalper ? '此处为 QQQ 逐批持仓，批次编号递增，不代表固定价格层级；US100 对冲净仓位见上方两腿账本。' : '此处为 QQQ 网格分格持仓；US100 对冲净仓位见上方两腿账本。';
    if (!data?.details_available) {$('detail-content').innerHTML = '<div class="empty">仓位与成交明细暂不可用</div>'; return;}
    if (!values.length) {$('detail-content').innerHTML = `<div class="empty">${trades ? '当前账户筛选下暂无模拟成交' : '当前账户筛选下暂无 QQQ ' + (scalper ? '批次' : '网格') + '持仓'}</div>`; return;}
    const headers = trades ? ['账户','时间 / 北京时间','场所 / 标的','方向','成交数量','成交价','成交额 / USDC','手续费 / USDC','原因','价格来源'] : ['账户',scalper ? '批次编号' : '网格编号','QQQ 数量','入场价格 / USDC','止盈价格 / USDC'];
    $('detail-content').innerHTML = table(headers, values.slice(page * size, (page + 1) * size).map(value => {
      const row = allRows().find(r => r.name === value.scenario);
      const items = trades ? [E(rowLabel(value.scenario)),M.date(value.ts),E(`${value.venue || '—'} / ${value.symbol || '—'}`),E(({buy:'买入',sell:'卖出',BUY:'买入',SELL:'卖出'}[value.side] || value.side || '—')),M.number(value.qty,6),M.number(value.price,4),M.number(value.notional,2),M.number(value.fee,4),E(fillReason(value)),E(fillPricing(value))] : [E(rowLabel(value.scenario)),E(value.slot),M.number(value.qty,6),M.number(value.entry_price,4),M.number(value.tp_price,4)];
      return `<tr>${cells(headers,items,row)}</tr>`;
    }).join(''));
    $('pagination').hidden = false; $('previous').disabled = page === 0; $('next').disabled = (page + 1) * size >= values.length;
    $('page-label').textContent = `第 ${page + 1} / ${Math.ceil(values.length / size)} 页 · 已加载 ${values.length} 条`;
  }
  function render() {renderOverview(); renderCharts(); renderLedgers(); renderDetails(); status();}
  async function refresh() {
    clearTimeout(timer); controller?.abort();
    if (document.hidden) return;
    const request = controller = new AbortController(), timeout = setTimeout(() => request.abort(), 8000);
    $('refresh').disabled = true;
    try {
      const response = await fetch('/api/dashboard?range=' + encodeURIComponent(state.range), {signal:request.signal,cache:'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const next = await response.json();
      if (request !== controller || document.hidden) return;
      if (next.summary && (next.summary.kind !== 'qqq_hedge' || !Array.isArray(next.summary.scenarios))) throw new Error('Unexpected dashboard payload');
      if (data?.reset?.generation !== next.reset?.generation) {selectedTs = null; page = 0;}
      data = next; received = Date.now(); progressReceived = performance.now();
      serverAge = data.summary ? Math.max(0, (M.finite(data.server_ts) ? Number(data.server_ts) : received / 1000) - Number(data.summary.ts)) : 0;
      disconnected = false;
      if (data.summary) state = M.state(location.search, names());
      render();
    } catch (error) {
      if (request !== controller || document.hidden) return;
      disconnected = true;
      if (data) status();
      else {$('notice').hidden = false; $('notice').textContent = '暂时无法读取监控数据，页面将自动重试。'; $('status').textContent = '连接中断'; $('status').className = 'status error';}
    } finally {
      clearTimeout(timeout);
      if (request === controller) {$('refresh').disabled = false; if (!document.hidden) timer = setTimeout(refresh, 10000);}
    }
  }
  $('reset').onclick = () => {resetGeneration = data?.reset?.generation; $('reset-dialog').showModal();};
  $('reset-cancel').onclick = () => $('reset-dialog').close();
  $('reset-confirm').onclick = async () => {
    if (resetSending) return;
    resetSending = true; resetError = ''; $('reset-dialog').close(); resetStatus();
    const abort = new AbortController(), timeout = setTimeout(() => abort.abort(), 8000);
    try {
      const response = await fetch('/api/reset', {method:'POST',headers:{'Content-Type':'application/json','X-Reset-Token':data.reset_token},body:JSON.stringify({generation:resetGeneration}),signal:abort.signal});
      if (!response.ok) throw new Error();
      data.reset = (await response.json()).reset;
    } catch {resetError = '重置请求结果尚未确认，请刷新查看状态；不会自动重复提交。';}
    finally {clearTimeout(timeout); resetSending = false; resetStatus(); refresh();}
  };
  $('refresh').onclick = refresh;
  $('all-strategies').onclick = () => setState({strategy:'all'});
  $('strategy-select').onchange = event => setState({strategy:event.target.value});
  $('sample').oninput = event => {selectedTs = data?.history?.points[Number(event.target.value)]?.ts ?? null; renderCharts();};
  $('latest-sample').onclick = () => {selectedTs = null; renderCharts();};
  $('previous').onclick = () => {page--; renderDetails();}; $('next').onclick = () => {page++; renderDetails();};
  document.addEventListener('click', event => {
    const strategy = event.target.closest('[data-strategy]'); if (strategy) setState({strategy:strategy.dataset.strategy});
    const range = event.target.closest('[data-range]'); if (range) {setState({range:range.dataset.range}); selectedTs = null; refresh();}
    const view = event.target.closest('[data-view]'); if (view) setState({view:view.dataset.view});
  });
  document.querySelector('.tabs').addEventListener('keydown', event => {
    const tabs = Array.from(document.querySelectorAll('.tabs [data-view]')), at = tabs.indexOf(event.target);
    if (at < 0 || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (at + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    setState({view:tabs[next].dataset.view}); tabs[next].focus();
  });
  $('export').onclick = () => {
    const values = [['账户','成交编号','北京时间','场所','标的','方向','数量','成交价USDC','手续费USDC','成交额USDC','原因','价格来源','源报价时间','源报价数量','报价年龄秒','半点差百分比','源买价','源卖价','maker_model'],...filtered(data?.trades).map(r => [r.scenario,r.id,M.date(r.ts,false,true),r.venue,r.symbol,r.side,r.qty,r.price,r.fee,r.notional,fillReason(r),fillPricing(r),M.finite(r.quote_ts) ? M.date(r.quote_ts,false,true) : '',r.source_qty ?? '',r.quote_age_seconds ?? '',r.half_spread_percent ?? '',r.source_bid ?? '',r.source_ask ?? '',r.maker_model ?? ''])];
    const url = URL.createObjectURL(new Blob([M.csv(values)], {type:'text/csv;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = `qqq-hedge-trades-${state.strategy}-${Date.now()}.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  window.addEventListener('popstate', () => {state = M.state(location.search,names()); page = 0; selectedTs = null; render(); refresh();});
  let resize;
  new ResizeObserver(() => {clearTimeout(resize); resize = setTimeout(renderCharts,80);}).observe($('pnl-chart'));
  document.addEventListener('visibilitychange', () => {if (document.hidden) {clearTimeout(timer); controller?.abort();} else refresh();});
  setInterval(() => {if (!document.hidden) status();}, 1000);
  render(); refresh();
})(typeof window !== 'undefined' ? window : globalThis);
