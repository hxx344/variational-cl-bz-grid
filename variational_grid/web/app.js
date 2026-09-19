/* Read-only presentation. Amounts are calculated in Decimal on the server. */
(() => {
  'use strict';
  const M = window.GridModel, $ = id => document.getElementById(id), E = M.escape;
  let data = null, state = M.state(location.search, []), page = 0, selectedTs = null;
  let timer = null, controller = null, disconnected = false, received = 0, serverAge = 0;
  const names = () => data?.summary?.scenarios.map(r => r.name) || [];
  const index = name => Math.max(0, names().indexOf(name));
  const cls = name => `c${index(name) % 4}`;
  const color = name => M.colors[index(name) % 4];
  const filtered = rows => (rows || []).filter(r => state.strategy === 'all' || r.scenario === state.strategy);
  const scenarios = () => (data?.summary?.scenarios || []).filter(r => state.strategy === 'all' || r.name === state.strategy);
  const reason = value => ({take_profit:'净止盈',max_holding:'持仓到期',max_drawdown:'回撤停机'}[value] || value || '—');
  const label = M.gridLabel;
  const pnl = value => `<span class="${M.tone(value)}">${M.signed(value)}</span>`;

  function setState(change, push = true) {
    state = {...state, ...change}; page = 0;
    const q = new URLSearchParams();
    if (state.strategy !== 'all') q.set('strategy', state.strategy);
    q.set('range', state.range); q.set('view', state.view);
    if (push) history.pushState({}, '', '?' + q.toString());
    render();
  }

  function status() {
    if (!data) return;
    const age = data.summary ? Math.max(0, serverAge + (Date.now()-received)/1000) : null;
    const old = age !== null && age > Math.max(60, data.summary.poll_seconds*3);
    const runtime = data.runtime.status;
    const labels = {running:'模拟运行中',paused:'行情暂停',stopped:'模拟已停止',starting:'等待行情'};
    $('status').textContent = disconnected ? '页面连接中断' : old ? '行情已过期' : labels[runtime] || '状态未知';
    $('status').className = 'status' + (disconnected ? ' error' : old || runtime !== 'running' ? ' warn' : '');
    $('freshness').textContent = age === null ? '等待第一份有效行情' : `最近有效行情 ${M.date(data.summary.ts)} · ${Math.floor(age)} 秒前 · 每 ${data.summary.poll_seconds} 秒采样`;
    let notice = '';
    if (disconnected) notice = '无法连接监控服务，保留上次数据。页面会自动重试。';
    else if (runtime === 'paused') notice = `行情暂停，保留最近有效采样：${data.runtime.reason || '等待数据恢复'}`;
    else if (runtime === 'stopped') notice = '模拟进程已停止，以下为最后保存的数据。';
    else if (old) notice = '行情已经过期，以下盈亏和仓位估值停留在最后有效采样，请检查模拟服务日志。';
    else if (data.summary && !data.details_available) notice = '汇总数据可用，当前缺少对应报价，暂不显示仓位估值明细。';
    $('notice').textContent = notice; $('notice').hidden = !notice;
  }

  function renderStrategies() {
    const rows = data?.summary?.scenarios || [];
    if (!rows.length) { $('strategies').innerHTML = '<div class="empty initial">暂无有效采样。模拟服务开始接收行情后，三组结果会自动显示。</div>'; return; }
    $('strategies').innerHTML = rows.map(r => `<button class="strategy ${cls(r.name)} ${state.strategy === r.name ? 'selected' : ''}" data-strategy="${E(r.name)}" aria-pressed="${state.strategy === r.name}" aria-label="查看${E(label(r))}策略"><div class="strategy-head"><h3>${E(label(r))}</h3><span class="step">${r.open_pairs} / ${r.max_levels} 组持仓</span></div><p class="grid-conversion">当前一格 ${M.number(r.grid_step,6)} USDC/桶</p><span class="pnl-label">累计损益 / USDC</span><strong class="big-pnl ${M.tone(r.total_pnl_usdc)}">${M.signed(r.total_pnl_usdc)}<small>USDC</small></strong><div class="strategy-stats"><div><label>已实现</label><b class="${M.tone(r.realized_pnl_usdc)}">${M.signed(r.realized_pnl_usdc)}</b></div><div><label>持仓浮盈亏</label><b class="${M.tone(Number(r.total_pnl_usdc)-Number(r.realized_pnl_usdc))}">${M.signed(Number(r.total_pnl_usdc)-Number(r.realized_pnl_usdc))}</b></div><div><label>最大回撤</label><b>${M.number(Number(r.max_drawdown_fraction)*100,3)}%</b></div></div><div class="volume-stats"><div><label>累计成交额 / USDC</label><b>${M.number(r.turnover_usdc,2)}</b></div><div><label>累计成交量 / 桶</label><b>${M.number(r.volume_barrels,3)}</b></div></div><div class="mini-stats"><span>权益 <b>${M.number(r.equity_usdc,2)}</b></span><span>已平仓 <b>${r.closed_pairs} 组</b></span></div></button>`).join('');
    $('strategy-select').innerHTML = '<option value="all">全部策略</option>' + rows.map(r => `<option value="${E(r.name)}">${E(label(r))}</option>`).join('');
    $('strategy-select').value = state.strategy;
    const s = data.summary;
    $('experiment-info').textContent = `各组独立核算 · 成交量含双腿开平仓，自实验开始累计 · ${s.sample_count.toLocaleString()} 次共同采样 · 开始于 ${M.date(Date.parse(s.started_utc)/1000)}`;
    $('source-window').textContent = `中枢数据窗口：${M.date(Date.parse(s.history_start_utc)/1000)} 至 ${M.date(Date.parse(s.history_end_utc)/1000)}。历史高低点保留，行情缺口断线显示；短期结果不足以判断长期优劣。`;
  }

  function drawChart(id, series, allValues, zero) {
    const host = $(id), points = data?.history.points || [];
    if (!points.length) { host.innerHTML = '<div class="empty">等待有效行情后绘制曲线</div>'; return; }
    const width = Math.max(260, host.clientWidth), height = host.clientHeight;
    const left = 56, right = width-20, top = 18, bottom = height-33;
    const [lo,hi] = M.domain(allValues,zero), first = points[0].ts, last = points.at(-1).ts;
    const x = t => left+(t-first)/Math.max(1,last-first)*(right-left);
    const y = v => bottom-(v-lo)/(hi-lo)*(bottom-top);
    const digits = hi-lo < .02 ? 4 : hi-lo < 5 ? 3 : 2;
    let content = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${E(host.getAttribute('aria-label'))}"><title>${E(host.getAttribute('aria-label'))}，${M.date(first)} 至 ${M.date(last)}</title>`;
    for (let i=0;i<4;i++) {
      const value = lo+(hi-lo)*i/3;
      content += `<line class="gridline" x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left-9}" y="${y(value)+4}" text-anchor="end">${M.number(value,digits)}</text>`;
    }
    if (zero && lo<0 && hi>0) content += `<line class="zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/>`;
    const ticks = width>600 ? 4 : 2;
    for(let i=0;i<ticks;i++) {
      const t=first+(last-first)*i/(ticks-1);
      content += `<text x="${x(t)}" y="${height-8}" text-anchor="${i===0?'start':i===ticks-1?'end':'middle'}">${M.date(t,true)}</text>`;
    }
    for(const s of series) content += `<path d="${M.path(points,s.get,x,y)}" stroke="${s.color}" ${s.dashed?'stroke-dasharray="5 5"':''}/>`;
    const selected=pointIndex(), point=points[selected];
    content += `<line class="cursor" x1="${x(point.ts)}" x2="${x(point.ts)}" y1="${top}" y2="${bottom}"/>`;
    for(const s of series) if(M.finite(s.get(point))) content += `<circle cx="${x(point.ts)}" cy="${y(s.get(point))}" r="3.5" fill="${s.color}" stroke="white" stroke-width="1.5"/>`;
    content+='</svg>'; host.innerHTML=content;
    host.onclick = event => {
      const ratio=Math.max(0,Math.min(1,(event.clientX-host.getBoundingClientRect().left-left)/(right-left)));
      const wanted=first+ratio*(last-first);
      selectedTs=points.reduce((a,b)=>Math.abs(b.ts-wanted)<Math.abs(a.ts-wanted)?b:a).ts;
      renderCharts();
    };
  }

  function pointIndex() {
    const points=data?.history.points || [];
    if(!points.length) return 0;
    if(selectedTs===null) return points.length-1;
    let result=0;
    points.forEach((p,i)=>{if(Math.abs(p.ts-selectedTs)<Math.abs(points[result].ts-selectedTs)) result=i;});
    return result;
  }

  function renderCharts() {
    const points=data?.history.points || [], rows=data?.summary?.scenarios || [];
    const series=scenarios().map(r=>({color:color(r.name),get:p=>p.pnl[data.history.names.indexOf(r.name)]}));
    drawChart('pnl-chart',series,points.flatMap(p=>p.pnl),true);
    drawChart('spread-chart',[{color:'#416c97',get:p=>p.spread},{color:'#b47818',get:p=>p.center,dashed:true}],points.flatMap(p=>[p.spread,p.center]),false);
    $('pnl-legend').innerHTML=scenarios().map(r=>`<span class="${cls(r.name)}"><i class="key"></i>${E(label(r))} <b>${M.signed(r.total_pnl_usdc)}</b></span>`).join('');
    $('sample').max=Math.max(0,points.length-1); $('sample').value=pointIndex(); $('sample').disabled=!points.length;
    const point=points[pointIndex()];
    $('sample-time').textContent=point?M.date(point.ts):'—';
    $('sample-values').textContent=point?scenarios().map(r=>`${label(r)}：${M.signed(point.pnl[data.history.names.indexOf(r.name)])}`).join('　'):'';
    const n=data?.history.source_count || 0;
    const shown={'1h':'1 小时','24h':'24 小时','7d':'7 天'}[data?.history.range] || '24 小时';
    $('history-note').textContent=`当前图表：最近 ${shown}，截至最后有效行情。${n.toLocaleString()} 次有效采样${n>points.length?'，按区间保留高低点，绘制 '+points.length+' 点':''}。缺失行情断线；点击图表或拖动采样条查看。`;
    const s=data?.summary, r=rows[0];
    $('spread-value').textContent=M.number(r?.spread_bz_minus_cl); $('center-value').textContent=M.number(s?.center_7d);
    $('deviation').textContent=r?`偏离 ${M.signed(r.deviation,3)}`:'—';
    $('cl-mark').textContent=M.number(r?.cl_mark); $('bz-mark').textContent=M.number(r?.bz_mark);
    document.querySelectorAll('[data-range]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.range===state.range));
  }

  function renderGrid() {
    if(data?.summary && !data.details_available) { $('grid-map').innerHTML='<div class="empty">网格明细暂不可用，请参考上方已保存的持仓组数。</div>'; return; }
    $('grid-map').innerHTML=scenarios().map(r=>{
      const lots=(data.positions||[]).filter(p=>p.scenario===r.name), slots=[];
      for(let level=1;level<=r.max_levels;level++) {
        const lot=lots.find(l=>l.level===level);
        const title=lot?`第 ${level} 格，${lot.direction===1?'CL 空 / BZ 多':'CL 多 / BZ 空'}，每腿 ${lot.qty} 桶`:`第 ${level} 格，空闲`;
        slots.push(`<button class="slot ${lot?'occupied':''}" data-slot-scenario="${E(r.name)}" ${lot?'':'disabled'} aria-label="${E(title)}" title="${E(title)}">${level}</button>`);
      }
      return `<div class="grid-row ${cls(r.name)}"><div class="grid-name">${E(label(r))}<span>${lots.length} / ${r.max_levels} 组 · 每腿 ${M.number(r.quantity_barrels,2)} 桶</span></div><div class="slots">${slots.join('')}</div><span class="slot-caption">CL ${Number(r.cl_barrels)>=0?'多':'空'} ${M.number(Math.abs(Number(r.cl_barrels)),2)} 桶 / BZ ${Number(r.bz_barrels)>=0?'多':'空'} ${M.number(Math.abs(Number(r.bz_barrels)),2)} 桶</span></div>`;
    }).join('') || '<div class="empty">尚无网格数据</div>';
  }

  function legs(r, exit=false) {
    return ['CL','BZ'].map((symbol,i)=>{
      const long = i===0?r.direction===-1:r.direction===1;
      const price=r[(exit?'exit_':'entry_')+symbol.toLowerCase()];
      return `<span class="leg"><b>${symbol}</b><span class="side ${long?'long':'short'}">${long?'多':'空'}</span>${M.number(r.qty,3)} 桶 <span class="secondary">${exit?'平仓':'开仓'}价 ${M.number(price)}</span></span>`;
    }).join('');
  }

  function renderDetails() {
    const positions=filtered(data?.positions), trades=filtered(data?.trades), isTrades=state.view==='trades';
    $('positions-count').textContent=scenarios().reduce((n,r)=>n+r.open_pairs,0);
    $('trades-count').textContent=scenarios().reduce((n,r)=>n+r.closed_pairs,0);
    document.querySelectorAll('.tabs [data-view]').forEach(b=>{
      const selected=b.dataset.view===state.view;
      b.setAttribute('aria-selected',selected); b.tabIndex=selected?0:-1; b.id='tab-'+b.dataset.view;
    });
    $('detail-content').setAttribute('aria-labelledby','tab-'+state.view);
    $('export').hidden=!isTrades; $('export').disabled=!trades.length;
    if(state.view==='parameters') {
      $('detail-content').innerHTML='<div class="table-wrap" tabindex="0" aria-label="参数对照表"><table class="parameters-table"><thead><tr><th>策略</th><th>当前一格 / USDC/桶</th><th>初始资金 / USDC</th><th>每腿桶数</th><th>最多组数</th><th>杠杆估算</th><th>手续费 / 滑点 bp</th><th>最长持仓</th><th>状态</th></tr></thead><tbody>'+scenarios().map(r=>`<tr><td class="row-name ${cls(r.name)}">${E(label(r))}</td><td>${M.number(r.grid_step,6)}</td><td>${M.number(r.initial_balance_usdc,2)}</td><td>${M.number(r.quantity_barrels,3)}</td><td>${r.max_levels}</td><td>${M.number(r.paper_leverage,1)} 倍</td><td>${M.number(r.fee_bps,1)} / ${M.number(r.slippage_bps,1)}</td><td>${r.max_holding_hours ?? '—'} 小时</td><td>${r.halted?'回撤停机':r.skip_reason==='zero_center'?'中枢为零，暂停开仓':r.open_allowed?'正常':'只减仓'}</td></tr>`).join('')+'</tbody></table></div>';
      $('pagination').hidden=true; $('detail-note').textContent='百分比基于七日平均价差中枢的绝对值。当前一格随中枢更新；每笔净止盈目标按开仓时的一格固定。中枢为零时暂停开仓。保证金为本地估算。'; return;
    }
    const rows=isTrades?trades:positions, size=15, pages=Math.max(1,Math.ceil(rows.length/size));
    page=Math.min(page,pages-1);
    if(!data?.details_available || !rows.length) $('detail-content').innerHTML=`<div class="empty">${!data?.details_available?'等待完整行情记录':isTrades?'暂无已平仓记录。持仓浮盈亏可在“当前仓位”查看。':'当前筛选下没有未平仓的配对仓位。'}</div>`;
    else $('detail-content').innerHTML=`<div class="table-wrap" tabindex="0" aria-label="${isTrades?'已平仓':'当前仓位'}明细"><table class="records-table"><thead><tr><th>策略 / 层级</th><th>双腿仓位与开仓价</th>${isTrades?'<th>平仓价</th>':''}<th>开仓时间</th>${isTrades?'<th>平仓时间 / 原因</th>':'<th>净止盈目标 / USDC</th><th>保证金估算 / USDC</th>'}<th class="numeric">${isTrades?'净损益':'浮盈亏'} / USDC</th></tr></thead><tbody>`+rows.slice(page*size,(page+1)*size).map(r=>{
      const s=data.summary.scenarios.find(x=>x.name===r.scenario), amount=isTrades?r.net_pnl:r.unrealized_pnl_usdc;
      return `<tr><td data-label="策略 / 层级" class="row-name ${cls(r.scenario)}">${E(label(s))}<span class="secondary">第 ${r.level} 格 / 编号 ${r.id}</span></td><td data-label="双腿仓位与开仓价" class="wide">${legs(r)}</td>${isTrades?`<td data-label="平仓价">CL ${M.number(r.exit_cl)}<span class="secondary">BZ ${M.number(r.exit_bz)}</span></td>`:''}<td data-label="开仓时间">${M.date(r.opened)}</td>${isTrades?`<td data-label="平仓时间 / 原因">${M.date(r.closed)}<span class="secondary">${E(reason(r.reason))}</span></td>`:`<td data-label="净止盈目标 / USDC">${M.number(r.target_pnl_usdc,6)}</td><td data-label="保证金估算 / USDC">${M.number(Number(r.qty)*(Number(s.cl_mark)+Number(s.bz_mark))/Number(s.paper_leverage),2)}</td>`}<td data-label="${isTrades?'净损益':'浮盈亏'} / USDC" class="numeric pnl-cell">${pnl(amount)}</td></tr>`;
    }).join('')+'</tbody></table></div>';
    $('pagination').hidden=pages<=1;
    $('page-label').textContent=`${rows.length} 条 · 第 ${page+1} / ${pages} 页`;
    $('previous').disabled=page===0; $('next').disabled=page===pages-1;
    $('detail-note').textContent=isTrades?`每条为一组双腿合计净损益，已计开平手续费与模拟滑点。每策略最多加载最近 ${data?.trade_limit_per_scenario || 100} 笔；导出包含当前筛选下已加载的 ${rows.length} 笔。`:'每条对应一组 CL + BZ 配对仓位，浮盈亏含预计退出成本。价格单位 USDC/桶，时间为北京时间。';
  }

  function render() {
    const active=document.activeElement, strategyFocus=active?.dataset?.strategy;
    renderStrategies(); renderCharts(); renderGrid(); renderDetails(); status();
    if(strategyFocus) Array.from(document.querySelectorAll('[data-strategy]')).find(b=>b.dataset.strategy===strategyFocus)?.focus({preventScroll:true});
  }

  async function refresh() {
    clearTimeout(timer); controller?.abort(); controller=new AbortController();
    const request=controller, timeout=setTimeout(()=>request.abort(),8000);
    $('refresh').disabled=true;
    try {
      const response=await fetch('/api/dashboard?range='+encodeURIComponent(state.range),{signal:request.signal,cache:'no-store'});
      if(!response.ok) throw new Error('HTTP '+response.status);
      const next=await response.json();
      if(request!==controller) return;
      data=next; received=Date.now(); serverAge=data.summary?data.server_ts-data.summary.ts:0; disconnected=false;
      // Preserve URL state while waiting for the first published sample.
      if(data.summary) state=M.state(location.search,names());
      render();
    } catch(error) {
      if(request!==controller) return;
      disconnected=true;
      if(data) status();
      else { $('notice').hidden=false; $('notice').textContent='暂时无法读取监控数据，页面将自动重试。'; $('status').textContent='连接中断'; $('status').className='status error'; }
    } finally {
      clearTimeout(timeout);
      if(request===controller) { $('refresh').disabled=false; if(!document.hidden) timer=setTimeout(refresh,10000); }
    }
  }

  $('refresh').addEventListener('click',refresh);
  document.querySelector('.tabs').addEventListener('keydown',event=>{
    const tabs=Array.from(document.querySelectorAll('.tabs [data-view]')), at=tabs.indexOf(event.target);
    if(at<0 || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(at+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    setState({view:tabs[next].dataset.view});tabs[next].focus();
  });
  $('all-strategies').onclick=()=>setState({strategy:'all'});
  $('strategy-select').onchange=event=>setState({strategy:event.target.value});
  $('sample').oninput=event=>{selectedTs=data.history.points[Number(event.target.value)]?.ts ?? null;renderCharts();};
  $('previous').onclick=()=>{page--;renderDetails();}; $('next').onclick=()=>{page++;renderDetails();};
  document.addEventListener('click',event=>{
    const strategy=event.target.closest('[data-strategy]');
    if(strategy) setState({strategy:strategy.dataset.strategy});
    const range=event.target.closest('[data-range]');
    if(range) {setState({range:range.dataset.range});selectedTs=null;refresh();}
    const view=event.target.closest('[data-view]');
    if(view) setState({view:view.dataset.view});
    const slot=event.target.closest('[data-slot-scenario]');
    if(slot && !slot.disabled) {setState({strategy:slot.dataset.slotScenario,view:'positions'});$('details').scrollIntoView({block:'start'});}
  });
  $('export').onclick=()=>{
    const rows=[['策略','编号','层级','方向','每腿桶数','CL开仓价','BZ开仓价','CL平仓价','BZ平仓价','开仓北京时间','平仓北京时间','净损益USDC','平仓原因'],...filtered(data.trades).map(r=>[r.scenario,r.id,r.level,r.direction===1?'CL空/BZ多':'CL多/BZ空',r.qty,r.entry_cl,r.entry_bz,r.exit_cl,r.exit_bz,M.date(r.opened,false,true),M.date(r.closed,false,true),r.net_pnl,reason(r.reason)])];
    const url=URL.createObjectURL(new Blob([M.csv(rows)],{type:'text/csv;charset=utf-8'}));
    const link=document.createElement('a');link.href=url;link.download=`cl-bz-trades-${state.strategy}-${Date.now()}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  window.addEventListener('popstate',()=>{state=M.state(location.search,names());page=0;render();refresh();});
  let resize;
  new ResizeObserver(()=>{clearTimeout(resize);resize=setTimeout(renderCharts,80);}).observe($('pnl-chart'));
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(timer);controller?.abort();}else refresh();});
  setInterval(status,1000);refresh();
})();
