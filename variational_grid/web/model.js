(function (root) {
  'use strict';
  const colors = ['#285de5', '#098477', '#b47818', '#7552a1'];
  const finite = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const number = (v, digits = 4) => finite(v) ? Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
  const signed = (v, digits = 4) => finite(v) ? (Number(v) > 0 ? '+' : '') + number(v, digits) : '—';
  const tone = v => !finite(v) || Number(v) === 0 ? 'neutral' : Number(v) > 0 ? 'gain' : 'loss';
  const gridLabel = row => row?.grid_step_percent != null ? `间距 ${Number(row.grid_step_percent)}%` : `间距 ${number(row?.grid_step, 2)} USDC/桶`;
  const levelLabel = row => row?.grid_limit_enabled === false || row?.max_levels === null ? '格数不限' : `每侧 ${row?.max_levels ?? '—'} 层`;
  const rangeLabel = row => row?.grid_limit_enabled === false || row?.max_levels === null ? '无固定覆盖范围' : finite(row?.grid_range_percent) ? `上下各 ${Number(row.grid_range_percent)}% · 总跨度 ${Number(row.grid_span_percent)}%` : levelLabel(row);
  const boundsLabel = row => row?.grid_limit_enabled === false || row?.max_levels === null ? '随价差向外扩展 · 每格一组' : `${number(row?.grid_lower,4)} ～ ${number(row?.grid_upper,4)} USDC/桶`;
  function gridWindow(row, lots, requestedPage = 0) {
    const size=60, highest=lots.reduce((n, lot)=>Math.max(n, Number(lot.level)||0),0);
    const unbounded=row?.grid_limit_enabled === false || row?.max_levels === null;
    const extent=unbounded ? Math.ceil((highest+1)/size)*size : Math.max(highest, Number(row?.max_levels)||0);
    const pages=Math.max(1,Math.ceil(extent/size)), page=Math.max(0,Math.min(pages-1,Math.floor(Number(requestedPage)||0)));
    return {start:page*size+1,end:Math.min(extent,(page+1)*size),page,pages,highest};
  }
  const limitLabel = (row, field = 'entry_notional_limit_usdc') => row?.margin_limit_enabled === false ? '金额不限' : finite(row?.[field]) ? number(row[field], 2) + ' USDC' : '—';
  const escape = v => String(v ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  function date(ts, short = false, year = false) {
    if (!finite(ts)) return '—';
    return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', ...(year ? {year:'numeric'} : {}), month:'2-digit',day:'2-digit', hour:'2-digit', minute:'2-digit', ...(short ? {} : {second:'2-digit'}), hourCycle:'h23'}).format(new Date(Number(ts) * 1000));
  }
  function state(search, names) {
    const q = new URLSearchParams(search);
    return { strategy: names.includes(q.get('strategy')) ? q.get('strategy') : 'all', range: ['1h','24h','7d'].includes(q.get('range')) ? q.get('range') : '24h', view: ['positions','trades','parameters'].includes(q.get('view')) ? q.get('view') : 'positions' };
  }
  function domain(values, zero = false) {
    const nums = values.filter(finite).map(Number);
    if (!nums.length) return [-1, 1];
    if (zero) nums.push(0);
    let lo = Math.min(...nums), hi = Math.max(...nums);
    const pad = Math.max((hi-lo)*0.12, .01);
    return [lo-pad,hi+pad];
  }
  function path(points, accessor, x, y) {
    let previous = null;
    return points.map(p => {
      const value = accessor(p);
      if (!finite(value)) { previous = null; return ''; }
      const prefix = previous === null || p.segment !== previous ? 'M' : 'L';
      previous = p.segment;
      return `${prefix}${x(p.ts).toFixed(2)},${y(Number(value)).toFixed(2)}`;
    }).join(' ');
  }
  function csv(rows) {
    return '\ufeff' + rows.map(row => row.map(value => {
      let text = String(value ?? '');
      if (/^[=+@\-\t\r]/.test(text) && !/^-?\d+(\.\d+)?$/.test(text)) text = "'" + text;
      return '"' + text.replace(/"/g, '""') + '"';
    }).join(',')).join('\r\n');
  }
  const model = { colors, finite, number, signed, tone, gridLabel, levelLabel, rangeLabel, boundsLabel, gridWindow, limitLabel, escape, date, state, domain, path, csv };
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  else root.GridModel = model;
})(typeof window !== 'undefined' ? window : globalThis);
