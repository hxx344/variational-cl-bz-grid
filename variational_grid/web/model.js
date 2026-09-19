(function (root) {
  'use strict';
  const colors = ['#285de5', '#098477', '#b47818', '#7552a1'];
  const finite = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const number = (v, digits = 4) => finite(v) ? Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
  const signed = (v, digits = 4) => finite(v) ? (Number(v) > 0 ? '+' : '') + number(v, digits) : '—';
  const tone = v => !finite(v) || Number(v) === 0 ? 'neutral' : Number(v) > 0 ? 'gain' : 'loss';
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
  const model = { colors, finite, number, signed, tone, escape, date, state, domain, path, csv };
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  else root.GridModel = model;
})(typeof window !== 'undefined' ? window : globalThis);
